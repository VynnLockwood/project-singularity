"""Team memory: comment -> SQL pairs that people accepted and ran. A pair needs a second confirmation to count."""
import datetime
import json
import re

import psycopg
from django.utils import timezone
from pgvector.django import CosineDistance
from psycopg import sql as psql

from schema.conn import ro_connection
from schema.query import _START
from search import embedder

from .context import memory_text
from .models import QueryExample

LEARN_MATCH = 0.97     # cosine at which a new pair counts as the same question as a stored one
EXPLAIN_TIMEOUT_MS = 2000
_QUOTED = re.compile(r"(\"(?:[^\"]|\"\")*\"|'(?:[^']|'')*')")


def _today() -> datetime.date:
    return datetime.date.today()


def normalize_sql(text: str) -> str:
    """Whitespace collapsed, a trailing `;` dropped, unquoted words lower-cased. Quoted names and strings stay as written."""
    parts = _QUOTED.split(text.strip())
    out = "".join(p if i % 2 else re.sub(r"\s+", " ", p).lower() for i, p in enumerate(parts)).strip()
    return out.rstrip(";").strip()


def select_problem(text: str) -> str | None:
    """Why this is not one plain SELECT/WITH statement (the rule run_select applies); None when it is."""
    if not isinstance(text, str) or not _START.match(text):
        return "only SELECT queries are allowed"
    if ";" in text.strip().rstrip(";"):
        return "only a single statement is allowed"
    return None


def explains(schema: str, text: str) -> str | None:
    """None when the database can plan `text` read-only and the plan changes nothing; otherwise the reason."""
    return _plan(schema, text)[0]


def _plan(schema: str, text: str) -> tuple[str | None, bool]:
    """(reason, definite). `definite` is False for a failure that says nothing about the SQL (connection lost, timeout)."""
    problem = select_problem(text)
    if problem:
        return problem, True
    body = text.strip().rstrip(";")
    try:
        with ro_connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute(f"SET LOCAL statement_timeout = {EXPLAIN_TIMEOUT_MS}")
            conn.execute(psql.SQL("SET LOCAL search_path = {}").format(psql.Identifier(schema)))
            plan = conn.execute("EXPLAIN (FORMAT JSON) " + body).fetchone()
    except psycopg.Error as exc:
        return (exc.diag.message_primary or str(exc)), (exc.sqlstate or "").startswith("42")  # 42xxx: syntax or unknown name
    if "ModifyTable" in json.dumps(next(iter(plan.values()))):
        return "the query would change data", True
    return None, True


def _vector(comments: list[str], kind: str) -> list[float]:
    return embedder.embed([memory_text(comments)], kind)[0]


def _value_set(values: list[dict]) -> set[tuple]:
    return {(v.get("table"), v.get("column"), v.get("value")) for v in values if isinstance(v, dict)}


def learn(user, schema: str, comments: list[str], text: str, values: list[dict]) -> QueryExample:
    problem = explains(schema, text)
    if problem:
        raise ValueError(problem)
    norm = normalize_sql(text)
    vec = _vector(comments, "passage")
    confirmation = {"user": user.pk, "day": _today().isoformat()}
    same = (QueryExample.objects.filter(schema=schema, sql_norm=norm)
            .annotate(dist=CosineDistance("embedding", vec)).order_by("dist").first())
    if same is None or 1 - same.dist < LEARN_MATCH:
        return QueryExample.objects.create(
            schema=schema, comments="\n".join(comments[-2:]), sql=text.strip().rstrip(";").strip(), sql_norm=norm,
            values=values, embedding=vec, confirmations=[confirmation], created_by=user)
    if confirmation not in same.confirmations:
        same.confirmations = same.confirmations + [confirmation]
    same.use_count += 1
    if same.status == "candidate" and len(same.confirmations) >= 2:   # distinct (user, day) pairs
        same.status = "active"
    same.save(update_fields=["confirmations", "use_count", "status"])
    return same


def _nearest(schema: str, comments: list[str], limit: int):
    vec = _vector(comments, "query")
    return (QueryExample.objects.filter(schema=schema, status="active")
            .annotate(dist=CosineDistance("embedding", vec)).order_by("dist")[:limit])


def _still_valid(ex: QueryExample) -> bool:
    """False (and the example is deleted) when its SQL no longer runs, e.g. the schema changed."""
    problem, definite = _plan(ex.schema, ex.sql)
    if problem is None or not definite:   # a dropped connection must not wipe the team's memory
        return True
    ex.delete()
    return False


def _touch(ex: QueryExample) -> None:
    QueryExample.objects.filter(pk=ex.pk).update(last_used=timezone.now())


def recall(schema: str, comments: list[str], values: list[dict], threshold: float) -> QueryExample | None:
    """A saved query to show at once: close enough in meaning AND found with the same stored values."""
    for ex in _nearest(schema, comments, 3):
        if 1 - ex.dist < threshold:
            return None
        if _value_set(ex.values) == _value_set(values) and _still_valid(ex):
            _touch(ex)
            return ex
    return None


def examples(schema: str, comments: list[str], k: int) -> list[QueryExample]:
    """The k nearest active examples that still run; stale ones are removed on the way."""
    out: list[QueryExample] = []
    for ex in _nearest(schema, comments, k * 2):
        if len(out) >= k:
            break
        if _still_valid(ex):
            _touch(ex)
            out.append(ex)
    return out
