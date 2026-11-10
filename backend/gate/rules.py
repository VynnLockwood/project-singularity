"""Rule evaluation (spec section 2). Pure functions: rules in, violations out.

A rule is a dict: {name, type, params, severity ('block'|'warn'), enabled, description}.
`block` violations reject a change; `warn` violations are shown to the approver.
"""
from __future__ import annotations

import copy
import re

from .conventions import ROLES, Conventions, strip_meta
from .operations import normalize_type

STYLES = ("camelCase", "snake_case", "PascalCase", "custom")
SEVERITIES = ("block", "warn")
RULE_TYPES = (
    "naming", "required_columns", "forbidden", "type_map",
    "reference_for_codes", "fk_naming", "business_key",
)

_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_STYLE_RE = {
    "camelCase": re.compile(r"^[a-z][a-zA-Z0-9]*$"),
    "snake_case": re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$"),
    "PascalCase": re.compile(r"^[A-Z][a-zA-Z0-9]*$"),
}


def words(name: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(name)]


def to_style(name: str, style: str, joiner: str | None = None) -> str:
    w = words(name) or [name.lower()]
    if style == "custom":
        return (joiner or "-").join(w)
    if style == "snake_case":
        return "_".join(w)
    cap = [x.capitalize() for x in w]
    if style == "PascalCase":
        return "".join(cap)
    return w[0] + "".join(cap[1:])


def matches_style(name: str, style: str, pattern: str | None = None) -> bool:
    """`style` is one of STYLES, or "custom" with a regex `pattern` (any convention, set through the MCP)."""
    if style == "custom":
        return bool(re.search(pattern or "", name))
    return bool(_STYLE_RE[style].match(name))


def validate_rule_definition(rtype: str, params, severity: str) -> list[str]:
    """Check a rule before it is stored. Returns a list of problems (empty = OK)."""
    errs: list[str] = []
    if rtype not in RULE_TYPES:
        return [f"unknown rule type {rtype!r}"]
    if severity not in SEVERITIES:
        errs.append("severity must be 'block' or 'warn'")
    if not isinstance(params, dict):
        return errs + ["params must be an object"]

    def regex(key, required=True):
        v = params.get(key)
        if v is None:
            if required:
                errs.append(f"params.{key} is required")
            return
        try:
            re.compile(v)
        except (re.error, TypeError):
            errs.append(f"params.{key} is not a valid regex")

    if rtype == "naming":
        if params.get("target") not in ("table", "column"):
            errs.append("params.target must be 'table' or 'column'")
        if params.get("style") not in STYLES:
            errs.append(f"params.style must be one of {list(STYLES)}")
        elif params["style"] == "custom":
            regex("pattern")
        ml = params.get("maxLength", 63)
        if not isinstance(ml, int) or not 1 <= ml <= 63:
            errs.append("params.maxLength must be an integer from 1 to 63")
        if not isinstance(params.get("reserved", []), list):
            errs.append("params.reserved must be a list")
    elif rtype == "required_columns":
        cols = params.get("columns")
        if not isinstance(cols, list) or not cols:
            errs.append("params.columns must be a non-empty list")
        else:
            from .operations import _check_column

            for c in cols:
                if not isinstance(c, dict):
                    errs.append("params.columns: column must be an object")
                    continue
                role = c.get("role")
                if role is not None and role not in ROLES:
                    errs.append(f"params.columns: role must be one of {list(ROLES)}")
                al = c.get("aliases", [])
                if not isinstance(al, list) or not all(isinstance(a, str) for a in al):
                    errs.append("params.columns: aliases must be a list of names")
                plain = strip_meta(c)
                if isinstance(plain.get("name"), str):
                    plain["name"] = plain["name"].replace("{table}", "t")
                _check_column(plain, errs, "params.columns", allow_references=False, allow_enum=False)
    elif rtype == "forbidden":
        ops = params.get("ops")
        if not isinstance(ops, list) or not ops:
            errs.append("params.ops must be a non-empty list of operation names")
    elif rtype == "type_map":
        regex("columnPattern")
        for key in ("allowedTypes", "forbiddenTypes"):
            if key in params and not isinstance(params[key], list):
                errs.append(f"params.{key} must be a list")
        if not params.get("allowedTypes") and not params.get("forbiddenTypes"):
            errs.append("give params.allowedTypes or params.forbiddenTypes")
    elif rtype == "reference_for_codes":
        regex("pattern")
    elif rtype == "fk_naming":
        if params.get("style") not in STYLES:
            errs.append(f"params.style must be one of {list(STYLES)}")
    elif rtype == "business_key":
        pass
    return errs


# --- evaluation -------------------------------------------------------------

def _columns_of(op: dict):
    """Yield (column_def_or_None, name, type, referenced, unique) for column-creating ops."""
    if op["op"] == "create_table":
        for c in op["columns"]:
            yield c
    elif op["op"] == "add_column":
        yield op["column"]


def evaluate(ops: list[dict], rules: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """Apply `rules` to `ops`. Returns (ops_out, violations, auto_added).

    ops_out is a copy of ops with auto-addable required columns inserted.
    """
    ops_out = copy.deepcopy(ops)
    conv = Conventions.from_rules(rules)
    violations: list[dict] = []
    auto_added: list[str] = []

    def add(rule, i, msg, suggestion=None):
        violations.append(
            {
                "rule": rule["name"],
                "severity": rule["severity"],
                "message": msg,
                "op_index": i,
                "suggestion": suggestion,
            }
        )

    for rule in rules:
        if not rule.get("enabled", True):
            continue
        p = rule["params"]
        rtype = rule["type"]

        if rtype == "naming":
            style, maxlen = p["style"], p.get("maxLength", 63)
            reserved = {r.lower() for r in p.get("reserved", [])}
            for i, op in enumerate(ops_out):
                names = []
                if p["target"] == "table":
                    if op["op"] == "create_table":
                        names.append(op["name"])
                    elif op["op"] == "rename_table":
                        names.append(op["new_name"])
                    elif op["op"] in ("create_view", "create_enum"):
                        names.append(op["name"])
                else:
                    if op["op"] == "create_table":
                        names += [c["name"] for c in op["columns"]]
                    elif op["op"] == "add_column":
                        names.append(op["column"]["name"])
                    elif op["op"] == "rename_column":
                        names.append(op["new_name"])
                for n in names:
                    what = p["target"]
                    if not matches_style(n, style, p.get("pattern")):
                        add(rule, i, f"{what} name {n!r} is not {style}", to_style(n, style, p.get("separator")))
                    elif len(n) > maxlen:
                        add(rule, i, f"{what} name {n!r} is longer than {maxlen} characters")
                    if n.lower() in reserved:
                        add(rule, i, f"{what} name {n!r} is a reserved word")

        elif rtype == "required_columns":
            auto = p.get("autoAdd", True)
            for i, op in enumerate(ops_out):
                if op["op"] != "create_table":
                    continue
                have = {c["name"]: c for c in op["columns"]}
                missing = []
                for req in p["columns"]:
                    req = {**strip_meta(req), "name": req["name"].replace("{table}", op["name"])}
                    cur = have.get(req["name"])
                    if cur is None:
                        if auto:
                            missing.append(copy.deepcopy(req))
                            auto_added.append(f"{op['name']}.{req['name']}")
                        else:
                            add(rule, i, f"table {op['name']!r} is missing required column {req['name']!r}")
                    elif normalize_type(cur["type"]) != normalize_type(req["type"]):
                        add(
                            rule, i,
                            f"column {op['name']}.{req['name']} must be {req['type']}, not {cur['type']}",
                            req["type"],
                        )
                if missing:
                    op["columns"] = missing + op["columns"]

        elif rtype == "forbidden":
            for i, op in enumerate(ops_out):
                if op["op"] in p["ops"]:
                    add(rule, i, rule.get("description") or f"operation {op['op']} is restricted")

        elif rtype == "type_map":
            pat = re.compile(p["columnPattern"])
            allowed = [normalize_type(t) for t in p.get("allowedTypes", [])]
            forbidden = [normalize_type(t) for t in p.get("forbiddenTypes", [])]
            for i, op in enumerate(ops_out):
                items = [(c["name"], c["type"]) for c in _columns_of(op)]
                if op["op"] == "alter_column_type":
                    items.append((op["column"], op["type"]))
                for name, typ in items:
                    if not pat.search(name):
                        continue
                    t = normalize_type(typ)
                    if allowed and t not in allowed:
                        add(rule, i, f"column {name!r} must use {' or '.join(allowed)}, not {t}", allowed[0])
                    if forbidden and t in forbidden:
                        add(rule, i, f"column {name!r} must not use {t}")

        elif rtype == "reference_for_codes":
            pat = re.compile(p["pattern"])
            for i, op in enumerate(ops_out):
                for c in _columns_of(op):
                    t = normalize_type(c["type"])
                    texty = t == "text" or t.startswith("varchar")
                    if (
                        texty and pat.search(c["name"]) and not c.get("references")
                        and not c.get("unique") and not c.get("primary_key")
                    ):
                        add(
                            rule, i,
                            f"code column {c['name']!r} is free text; reference a lookup table instead",
                        )

        elif rtype == "fk_naming":
            style = p["style"]
            for i, op in enumerate(ops_out):
                pairs = []
                for c in _columns_of(op):
                    if c.get("references"):
                        pairs.append((c["name"], c["references"]["table"], op.get("name") or op["table"]))
                if op["op"] == "add_foreign_key":
                    pairs.append((op["column"], op["ref_table"], op["table"]))
                for col, ref, own in pairs:
                    if ref == own:
                        continue
                    pk_names = conv.candidates("primary_key", ref)
                    expected = pk_names[0] if pk_names else to_style(f"{ref}_id", style, p.get("separator"))
                    if col != expected:
                        add(rule, i, f"foreign key column {col!r} should be named {expected!r}", expected)

        elif rtype == "business_key":
            for i, op in enumerate(ops_out):
                if op["op"] != "create_table":
                    continue
                if not any(c.get("unique") or (c.get("primary_key") and c["name"] not in conv.candidates("primary_key", op["name"])) for c in op["columns"]):
                    add(rule, i, f"table {op['name']!r} has no unique business key column (for example a code)")

    return ops_out, violations, auto_added
