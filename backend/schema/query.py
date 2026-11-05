"""Read-only SELECT for the `query` tool and API (spec section 3).

Defence in depth: a text check, a READ ONLY transaction, a statement timeout,
a row cap, and above all the read-only DB role, which cannot write at all.
"""
from __future__ import annotations

import re

import psycopg

from .conn import ro_connection
from .util import jsonable

_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


class QueryError(Exception):
    pass


def run_select(text: str, *, limit: int = 500, timeout_ms: int = 5000) -> dict:
    if not isinstance(text, str) or not _START.match(text):
        raise QueryError("only SELECT queries are allowed")
    body = text.strip().rstrip(";").strip()
    if ";" in body:
        raise QueryError("only a single statement is allowed")
    limit = max(1, min(int(limit), 10_000))
    try:
        with ro_connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            conn.execute("SET LOCAL search_path = master")
            cur = conn.execute(body)
            if cur.description is None:
                raise QueryError("query returned no result set")
            columns = [d.name for d in cur.description]
            rows = cur.fetchmany(limit + 1)
    except psycopg.Error as exc:
        raise QueryError(exc.diag.message_primary or str(exc)) from exc
    truncated = len(rows) > limit
    return {
        "columns": columns,
        "rows": [[jsonable(r[c]) for c in columns] for r in rows[:limit]],
        "truncated": truncated,
    }
