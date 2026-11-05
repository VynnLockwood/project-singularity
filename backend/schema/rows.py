"""Paged, sortable, filterable reads of one `master` table for the grid editor (read-only role)."""
from __future__ import annotations

import psycopg
from psycopg import sql

from config.siconfig import get_config

from . import introspect
from .conn import ro_connection
from .util import jsonable

MAX_PAGE = 200
AUDIT = {"id", "createdAt", "updatedAt", "createdBy", "isActive"}


class RowsError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _describe(table: str) -> dict:
    d = introspect.describe_table(table)
    if d is None:
        raise RowsError("table not found", 404)
    return d


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _run(query: sql.Composable, params=()) -> list[dict]:
    limits = get_config()["limits"]
    try:
        with ro_connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute(f"SET LOCAL statement_timeout = {int(limits['queryTimeoutMs'])}")
            return conn.execute(query, params).fetchall()
    except psycopg.Error as exc:
        raise RowsError(exc.diag.message_primary or "query failed") from exc


def read_rows(table: str, *, offset: int = 0, limit: int = 50, sort: str | None = None,
              direction: str = "asc", filters: dict | None = None, include_inactive: bool = False) -> dict:
    d = _describe(table)
    cols = {c["name"]: c for c in d["columns"]}
    limit = max(1, min(int(limit), MAX_PAGE))
    offset = max(0, int(offset))
    if direction not in ("asc", "desc"):
        raise RowsError("direction must be asc or desc")
    if sort is not None and sort not in cols:
        raise RowsError(f"unknown column {sort!r}")

    where, params = [], []
    for col, text in (filters or {}).items():
        if col not in cols:
            raise RowsError(f"unknown column {col!r}")
        if not isinstance(text, str):
            raise RowsError("filters must be text")
        if text:
            where.append(sql.SQL("{}::text ILIKE %s").format(sql.Identifier(col)))
            params.append(_like(text))
    if "isActive" in cols and not include_inactive:
        where.append(sql.SQL('"isActive" IS DISTINCT FROM false'))
    clause = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where) if where else sql.SQL("")

    order_col = sort or ("id" if "id" in cols else next(iter(cols)))
    order = sql.SQL("{} {}").format(sql.Identifier(order_col), sql.SQL(direction.upper()))
    if "id" in cols and order_col != "id":
        order += sql.SQL(", id")

    t = sql.Identifier("master", table)
    total = _run(sql.SQL("SELECT count(*) AS n FROM {}{}").format(t, clause), params)[0]["n"]
    rows = _run(sql.SQL("SELECT * FROM {}{} ORDER BY {} LIMIT {} OFFSET {}").format(
        t, clause, order, sql.Literal(limit), sql.Literal(offset)), params)

    refs = {r["from"]["column"]: r["to"] for r in introspect.graph()["relations"] if r["from"]["table"] == table}
    return {
        "table": table, "total": total, "offset": offset, "limit": limit,
        "columns": [{
            "name": c["name"], "type": c["type"], "nullable": c["nullable"],
            "readOnly": c["identity"] or c["name"] in AUDIT,
            "references": refs.get(c["name"]),
        } for c in d["columns"]],
        "rows": [{k: jsonable(v) for k, v in r.items()} for r in rows],
    }


def label_column(columns: dict) -> str | None:
    """The column that best names a row: *name, then *code, then the first other text column."""
    texts = [n for n, c in columns.items()
             if n not in AUDIT and (c["type"] == "text" or c["type"].startswith("character varying"))]
    for suffix in ("name", "code"):
        for n in texts:
            if n.lower().endswith(suffix):
                return n
    return texts[0] if texts else None


def options(table: str, *, q: str = "", limit: int = 100) -> dict:
    """id + label pairs for a reference dropdown."""
    d = _describe(table)
    cols = {c["name"]: c for c in d["columns"]}
    if "id" not in cols:
        raise RowsError("this table has no id column to reference")
    label = label_column(cols)
    limit = max(1, min(int(limit), MAX_PAGE))
    t = sql.Identifier("master", table)
    label_expr = sql.Identifier(label) if label else sql.SQL("id::text")
    where, params = sql.SQL(""), []
    conds = []
    if q:
        conds.append(sql.SQL("{}::text ILIKE %s").format(label_expr))
        params.append(_like(q))
    if "isActive" in cols:
        conds.append(sql.SQL('"isActive" IS DISTINCT FROM false'))
    if conds:
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds)
    rows = _run(sql.SQL("SELECT id, {} AS label FROM {}{} ORDER BY label LIMIT {}").format(
        label_expr, t, where, sql.Literal(limit + 1)), params)
    return {
        "labelColumn": label, "truncated": len(rows) > limit,
        "options": [{"id": jsonable(r["id"]), "label": str(r["label"])} for r in rows[:limit]],
    }
