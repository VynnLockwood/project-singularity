"""Designer: one chat-model call per round that turns profiles and match candidates into a mapping plan.

Only profiles go to the model: headers, types, stats and at most `import.sampleValues` (never more than 5)
sample values per column, each cut to 80 chars. The `top` list is cut to the values that are already samples,
and the Matcher's row examples (incoming and existing values) never leave this process: only their counts do.

The reply must be one JSON object:
    {"mapping": <scripts mapping JSON>, "filters": {"<sheet>": [{"column", "op", "value"}]},
     "reasons": {"<sheet>" | "<sheet>.<header>": "<one line>"}, "questions": ["...", ...]}   # questions <= 3
Code fences are stripped. A reply that does not parse (or has the wrong JSON types) is retried once with the
error; a second failure raises DesignError("the model did not return a valid plan").
"""
from __future__ import annotations

import json
import re

from chat import providers
from chat.errors import ChatError
from config.siconfig import get_config

MAX_SAMPLES = 5
MAX_CHARS = 80
MAX_QUESTIONS = 3
FILTER_OPS = ("equals", "in", "not_empty")

SYSTEM = """You design how a spreadsheet is loaded into a PostgreSQL master-data database.
You see column profiles (never whole rows), the candidate target tables and columns a local matcher found, the
team's design rules, and maybe a recipe: a mapping that was approved before for files with the same headers.
Reply with ONE JSON object and nothing else, of exactly this shape:

{"mapping": {"tables": {"<sheet>": {
     "target": "<table>",                                   # an existing table, or a new snake_case name
     "columns": {"<target column>": "<sheet header>"},      # copied values
     "drop": ["<sheet header>"],                            # headers deliberately not loaded
     "refs": [{"column": "<integer FK column>", "parent": "<parent table>", "parent_key": "<unique column>",
               "source_column": "<sheet header holding the parent's key>", "external": true}],
     "add_columns": [{"name": "<new column>", "type": "<type>", "nullable": true, "comment": "..."}]}}},
 "filters": {"<sheet>": [{"column": "<sheet header>", "op": "equals" | "in" | "not_empty", "value": ...}]},
 "reasons": {"<sheet>": "<one line>", "<sheet>.<header>": "<one line>"},
 "questions": ["<question for the person>"]}

Rules for the mapping:
- The sheet names and headers are the source table and column names, exactly as given (case and spaces kept).
- Every header must be mapped to exactly one target column, dropped, or used as a ref's source_column.
- Every sheet must be in mapping.tables.
- refs with "external": true point at an existing table (parent = its name, parent_key = a unique column of it);
  the value in the sheet is matched against parent_key and the FK column gets the parent's id.
- add_columns is only for an existing target table, for a column it lacks; the new name may then be used in
  columns. A new target table gets its columns from columns (types come from the profiles).
- Target names are snake_case. Follow the team's rules.
Filters keep only the rows where every condition holds (AND). They compare the stripped raw cell text of the
sheet (before any conversion): "equals" takes a string value, "in" a list of strings, "not_empty" no value.
Rows that already exist in the target are skipped automatically: do not filter them out.
Give a one-line reason for each table and column decision.
Ask questions (at most 3) only when a choice genuinely needs the person; then the mapping may be empty.
When "Problems" are listed, they come from checking the last plan against the database: fix every one."""


class DesignError(Exception):
    """The Designer could not produce a plan (no model, the provider failed, or no valid JSON twice)."""


# ---------------------------------------------------------------- what the model sees

def _clip(v) -> str:
    return str(v)[:MAX_CHARS]


def model_profiles(profiles: list[dict], samples: int) -> list[dict]:
    """Profiles cut to what may reach the model: <= samples (<= 5) samples per column, top limited to them."""
    n = max(0, min(int(samples), MAX_SAMPLES))
    out = []
    for p in profiles or []:
        cols = []
        for c in p.get("columns") or []:
            kept = [_clip(s) for s in (c.get("samples") or [])[:n]]
            cols.append({
                "header": c.get("header"), "type": c.get("type"), "share": c.get("share"),
                "empty": c.get("empty"), "distinct": c.get("distinct"), "unique": c.get("unique"),
                "maxLength": c.get("maxLength"), "pattern": c.get("pattern"), "convert": c.get("convert"),
                "note": c.get("note"), "samples": kept,
                "top": [[_clip(v), cnt] for v, cnt in c.get("top") or [] if _clip(v) in kept],
            })
        out.append({"name": p.get("name"), "rows": p.get("rows"), "columns": cols})
    return out


def model_matches(matches: dict) -> dict:
    """Candidates and counts only: no row examples, no existing values."""
    out = {"searchOffline": bool((matches or {}).get("searchOffline")), "sheets": {}}
    recipe = (matches or {}).get("recipe")
    if recipe:
        out["recipe"] = {"mapping": recipe.get("mapping"), "filters": recipe.get("filters")}
    for name, s in ((matches or {}).get("sources") or {}).items():
        entry = {"tables": s.get("tables"), "columns": s.get("columns"), "refs": s.get("refs")}
        rows = s.get("rows")
        if rows:
            entry["rows"] = {"table": rows.get("table"), "alreadyExist": len(rows.get("exists") or []),
                             "mayExist": len(rows.get("maybe") or [])}
        out["sheets"][name] = entry
    return out


def _user_message(prompt, profiles, matches, rules_text, targets_text, answers, feedback, samples) -> str:
    parts = [
        "Request from the person:\n" + (prompt or "(none)"),
        "Sheets (column profiles):\n" + json.dumps(model_profiles(profiles, samples), ensure_ascii=False),
        "Matcher candidates ([table, column, score]):\n" + json.dumps(model_matches(matches), ensure_ascii=False),
    ]
    if (matches or {}).get("recipe"):
        parts.append("A recipe (approved before for this file shape) is in the candidates under \"recipe\": "
                     "start from it and repair only what the problems say has drifted.")
    parts.append("Existing tables that may be targets:\n" + (targets_text or "(none)"))
    parts.append("Team rules:\n" + (rules_text or "(none)"))
    if answers:
        lines = []
        for a in answers:
            for q in a.get("questions") or []:
                lines.append(f"Q: {q}")
            lines.append(f"A: {a.get('text', '')}")
        parts.append("The person's answers to your questions:\n" + "\n".join(lines))
    if feedback:
        parts.append("Problems found when the last plan was checked:\n" + "\n".join(f"- {f}" for f in feedback))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- the reply

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def strip_fences(text: str) -> str:
    m = _FENCE.search(text or "")
    return (m.group(1) if m else (text or "")).strip()


def check_plan(mapping, filters) -> list[str]:
    """Type problems of a mapping and filters (from the model or from the person); [] when the shapes are right."""
    problems = []
    if not isinstance(mapping, dict):
        return ["mapping must be a JSON object"]
    tables = mapping.get("tables", {})
    if not isinstance(tables, dict):
        problems.append("mapping.tables must be an object of sheet -> table mapping")
    else:
        for name, entry in tables.items():
            if not isinstance(entry, dict):
                problems.append(f"mapping.tables.{name} must be an object")
            elif "target" in entry and not isinstance(entry["target"], str):
                problems.append(f"mapping.tables.{name}.target must be a string")
    if not isinstance(filters, dict):
        problems.append("filters must be an object of sheet -> list of conditions")
        return problems
    for name, conds in filters.items():
        if not isinstance(conds, list):
            problems.append(f"filters.{name} must be a list")
            continue
        for i, c in enumerate(conds):
            if not isinstance(c, dict) or not isinstance(c.get("column"), str) or not isinstance(c.get("op"), str):
                problems.append(f"filters.{name}[{i}] must be {{column, op, value}} with text column and op")
            elif c["op"] not in FILTER_OPS:
                problems.append(f"filters.{name}[{i}].op must be one of {', '.join(FILTER_OPS)}")
            elif c["op"] == "in" and not isinstance(c.get("value"), list):
                problems.append(f"filters.{name}[{i}].value must be a list for 'in'")
    return problems


def parse(text: str) -> dict:
    """The plan in a reply; ValueError naming what is wrong."""
    body = strip_fences(text)
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        raise ValueError("the reply has no JSON object")
    try:
        data = json.loads(body[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"the JSON does not parse: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("the reply must be one JSON object")
    mapping = data.get("mapping") if data.get("mapping") is not None else {}
    filters = data.get("filters") if data.get("filters") is not None else {}
    reasons = data.get("reasons") if data.get("reasons") is not None else {}
    questions = data.get("questions") if data.get("questions") is not None else []
    problems = check_plan(mapping, filters)
    if not isinstance(reasons, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                for k, v in reasons.items()):
        problems.append("reasons must be an object of path -> text")
    if not isinstance(questions, list) or not all(isinstance(q, str) for q in questions):
        problems.append("questions must be a list of strings")
    if problems:
        raise ValueError("; ".join(problems))
    return {"mapping": mapping, "filters": filters, "reasons": reasons,
            "questions": [q.strip()[:500] for q in questions if q.strip()][:MAX_QUESTIONS]}


# ---------------------------------------------------------------- the call

def model_name(cfg: dict) -> str:
    return cfg["import"]["model"] or cfg["chat"]["defaultModel"]


def ask(prompt, profiles, matches, rules_text, targets_text, answers, feedback, api_key) -> dict:
    cfg = get_config()
    model = model_name(cfg)
    if not model:
        raise DesignError("no chat model is configured")
    samples = cfg["import"].get("sampleValues", MAX_SAMPLES)
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": _user_message(prompt, profiles, matches, rules_text, targets_text,
                                                          answers, feedback, samples)}]
    for attempt in range(2):
        try:
            reply = providers.complete(cfg["chat"]["provider"], messages, [], model, key=api_key)
        except ChatError as exc:
            raise DesignError(f"the chat model failed: {exc.message}") from None
        content = (reply or {}).get("content") or ""
        try:
            return parse(content)
        except ValueError as exc:
            if attempt:
                break
            messages = messages + [
                {"role": "assistant", "content": content[:4000]},
                {"role": "user", "content": f"That was not a valid plan: {exc}. Reply with only the JSON object "
                                            "of the shape described."}]
    raise DesignError("the model did not return a valid plan")
