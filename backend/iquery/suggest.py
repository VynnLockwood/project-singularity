"""Comment -> SQL suggestion: gather context, ask the model, check the answer before anyone sees it."""
import hashlib
import json
import re
import time
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from chat import providers
from chat.errors import ChatError
from config.siconfig import get_config

from . import memory
from .context import describe_for_prompt, find_values, pick_tables
from .models import SuggestCall

DEADLINE_S = 20
CACHE_S = 600

SYSTEM = (
    "You write PostgreSQL queries for a person who knows SQL. Rules: answer with exactly one SELECT (or WITH ... SELECT) "
    "statement, a single statement that changes nothing; use only the tables and columns listed, written exactly as "
    "listed in double quotes; never qualify names with a schema; the newest comment is the main request, while earlier "
    "comments and the SQL written so far are context to build on, so return the complete new query, not a fragment; "
    "use the values seen in the data when the comment names them; reply with the SQL only, no explanation, no markdown."
)


class SuggestError(Exception):
    """code: rate_limited | not_configured | provider_error | bad_request."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def clean_reply(text: str) -> str:
    """The SQL in a model reply: the first fenced block if there is one, else from the first SELECT/WITH line on."""
    text = text or ""
    fenced = re.search(r"```[a-zA-Z]*\n(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = re.search(r"^\s*(select|with)\b", text, re.I | re.M)
    if start:
        text = text[start.start():]
    return text.strip()


def _check_rate_limit(user, limit: int) -> None:
    since = timezone.now() - timedelta(minutes=1)
    if SuggestCall.objects.filter(user=user, created_at__gte=since).count() >= limit:
        raise SuggestError("rate_limited", "too many suggestions; wait a moment")
    SuggestCall.objects.create(user=user)


def _messages(schema, comments, sql_context, tables, values, examples) -> list[dict]:
    parts = ["Tables:\n" + (describe_for_prompt(schema, tables) or "(none found)")]
    if values:
        parts.append("Values seen in the data:\n" + "\n".join(
            f"- \"{v['table']}\".\"{v['column']}\" = '{v['value']}'" for v in values))
    if examples:
        parts.append("Examples of queries the team accepted:\n" + "\n\n".join(
            f"Comment: {e.comments}\nSQL: {e.sql}" for e in examples))
    if sql_context.strip():
        parts.append("SQL so far in the editor:\n" + sql_context.strip())
    if len(comments) > 1:
        parts.append("Earlier comments (oldest first):\n" + "\n".join(f"- {c}" for c in comments[:-1]))
    parts.append("Main request (newest comment):\n" + comments[-1])
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}]


def _model_name(cfg: dict) -> str:
    return cfg["intelligentQuery"]["model"] or cfg["chat"]["defaultModel"]


def _ask(cfg, messages, api_key) -> str:
    try:
        reply = providers.complete(cfg["chat"]["provider"], messages, [], _model_name(cfg), key=api_key)
    except ChatError as exc:
        code = "not_configured" if exc.code == "not_configured" else "rate_limited" if exc.code == "rate_limited" \
            else "provider_error"
        raise SuggestError(code, exc.message) from exc
    return clean_reply(reply.get("content", ""))


def _problem(schema: str, text: str) -> str | None:
    return memory.select_problem(text) or memory.explains(schema, text)


def suggest(user, schema: str, comments: list[str], sql_context: str, api_key: str = "") -> dict:
    """One suggestion, checked: a single SELECT that the database can plan read-only. At most one retry."""
    if not comments:
        raise SuggestError("bad_request", "comments are required")
    cfg = get_config()
    iq = cfg["intelligentQuery"]
    started = time.monotonic()
    _check_rate_limit(user, iq["ratePerMinute"])
    if not _model_name(cfg):
        raise SuggestError("not_configured", "no chat model is configured")
    key = "iq:" + hashlib.sha256(json.dumps(
        [schema, comments, sql_context, _model_name(cfg)], ensure_ascii=False).encode()).hexdigest()
    hit = cache.get(key)
    if hit is not None:
        return hit

    picked = pick_tables(schema, comments, iq["maxTables"])
    values = find_values(schema, picked["tables"], comments, iq["valueLookupMs"])
    examples = memory.examples(schema, comments, iq["examples"])
    messages = _messages(schema, comments, sql_context, picked["tables"], values, examples)

    text = _ask(cfg, messages, api_key)
    problem = _problem(schema, text)
    if problem and time.monotonic() - started < DEADLINE_S:
        messages += [{"role": "assistant", "content": text or "(empty)"},
                     {"role": "user", "content": f"That query failed: {problem}\nReturn a corrected query, SQL only."}]
        text = _ask(cfg, messages, api_key)
        problem = _problem(schema, text)
    elif problem:
        problem = "took too long"
    result = {"sql": None if problem else text, "source": "model", "tables": picked["tables"], "values": values,
              "searchOffline": picked["searchOffline"], "lowConfidence": not picked["confident"] and not picked["searchOffline"],
              "reason": problem}
    if not problem:
        cache.set(key, result, CACHE_S)
    return result


def recall_for(schema: str, comments: list[str]) -> dict:
    """The fast path: a saved query for the same question and the same stored values, or nothing."""
    iq = get_config()["intelligentQuery"]
    tables = pick_tables(schema, comments, iq["maxTables"])["tables"]
    values = find_values(schema, tables, comments, iq["valueLookupMs"])
    ex = memory.recall(schema, comments, values, iq["memoryHit"])
    return {"sql": ex.sql if ex else None, "source": "memory", "values": values}
