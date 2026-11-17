"""Read-only SELECT for the `query` tool and API (spec section 3).

Defence in depth: a text check, a READ ONLY transaction, a statement timeout,
a row cap, and above all the read-only DB role, which cannot write at all.

Per-database access: the user must reach `schema`, and for non-admins the SQL may name, and its plan read, only
databases they can read (`schema.isolation`); no catalog reads that list database names.
"""
from __future__ import annotations

import re

import psycopg
from psycopg import sql

from . import isolation
from .conn import ro_connection
from .isolation import QueryError  # noqa: F401  (re-exported: callers catch query.QueryError)
from .util import jsonable

_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def run_select(text: str, *, user, limit: int = 500, timeout_ms: int = 5000,
               schema: str = "master") -> dict:
    """Run one read-only SELECT in `schema` for `user` (required: every caller says whose reach applies).

    A database `user` cannot read raises `introspect.UnknownSchema` (the text of a missing one); SQL naming or
    planning onto a database outside their reach, or reading the catalogs, raises QueryError. Admins pass."""
    from accounts import access

    access.require(user, schema)
    if not isinstance(text, str) or not _START.match(text):
        raise QueryError("only SELECT queries are allowed")
    body = text.strip().rstrip(";").strip()
    if ";" in body:
        raise QueryError("only a single statement is allowed")
    limit = max(1, min(int(limit), 10_000))
    is_admin = access.is_admin(user)
    allowed = set() if is_admin else set(access.readable_schemas(user))
    isolation.check_text(body, allowed, is_admin=is_admin)
    try:
        with ro_connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            conn.execute(sql.SQL("SET LOCAL search_path = {}").format(sql.Identifier(schema)))
            isolation.check_plan(conn, body, allowed, is_admin=is_admin)
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
