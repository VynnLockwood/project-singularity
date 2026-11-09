"""Paged, sortable, filterable reads of one `master` table for the grid editor (read-only role)."""
from __future__ import annotations

import psycopg
from psycopg import sql

from config.siconfig import get_config

from . import introspect, roles
from .conn import ro_connection
from .util import jsonable

MAX_PAGE = 200


class RowsError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _describe(table: str, schema: str = "master") -> dict:
    d = introspect.describe_table(table, schema)
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
              direction: str = "asc", filters: dict | None = None, include_inactive: bool = False,
              schema: str = "master") -> dict:
    d = _describe(table, schema)
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
    conv = roles.conventions()
    role_cols = roles.roles_of(d, conv)
    hidden = roles.hidden_columns(conv)
    active = role_cols.get("is_active")
    if active and not include_inactive:
        where.append(sql.SQL("{} IS DISTINCT FROM false").format(sql.Identifier(active)))
    clause = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where) if where else sql.SQL("")

    pk = role_cols.get("primary_key")
    order_col = sort or pk or next(iter(cols))
    order = sql.SQL("{} {}").format(sql.Identifier(order_col), sql.SQL(direction.upper()))
    if pk and order_col != pk:
        order += sql.SQL(", {}").format(sql.Identifier(pk))

    t = sql.Identifier(schema, table)
    total = _run(sql.SQL("SELECT count(*) AS n FROM {}{}").format(t, clause), params)[0]["n"]
    rows = _run(sql.SQL("SELECT * FROM {}{} ORDER BY {} LIMIT {} OFFSET {}").format(
        t, clause, order, sql.Literal(limit), sql.Literal(offset)), params)

    refs = {x["from"]["column"]: x["to"] for x in introspect.graph(schema)["relations"] if x["from"]["table"] == table}
    return {
        "table": table, "total": total, "offset": offset, "limit": limit, "primaryKey": pk,
        "columns": [{
            "name": c["name"], "type": c["type"], "nullable": c["nullable"],
            "readOnly": c["identity"] or c["name"] in hidden or c["name"] in role_cols.values(),
            "references": refs.get(c["name"]),
        } for c in d["columns"]],
        "rows": [{k: jsonable(v) for k, v in row.items()} for row in rows],
    }


def label_column(columns: dict, skip: set[str] | frozenset[str] = frozenset()) -> str | None:
    """The column that best names a row: *name, then *code, then the first other text column."""
    texts = [n for n, c in columns.items()
             if n not in skip and (c["type"] == "text" or c["type"].startswith("character varying"))]
    for suffix in ("name", "code"):
        for n in texts:
            if n.lower().endswith(suffix):
                return n
    return texts[0] if texts else None


def options(table: str, *, q: str = "", limit: int = 100, schema: str = "master") -> dict:
    """id + label pairs for a reference dropdown."""
    d = _describe(table, schema)
    cols = {c["name"]: c for c in d["columns"]}
    conv = roles.conventions()
    role_cols = roles.roles_of(d, conv)
    pk = role_cols.get("primary_key")
    if pk is None:
        raise RowsError("this table has no primary key column to reference")
    label = label_column(cols, set(role_cols.values()) | roles.hidden_columns(conv))
    limit = max(1, min(int(limit), MAX_PAGE))
    t = sql.Identifier(schema, table)
    label_expr = sql.Identifier(label) if label else sql.SQL("{}::text").format(sql.Identifier(pk))
    where, params = sql.SQL(""), []
    conds = []
    if q:
        conds.append(sql.SQL("{}::text ILIKE %s").format(label_expr))
        params.append(_like(q))
    active = role_cols.get("is_active")
    if active:
        conds.append(sql.SQL("{} IS DISTINCT FROM false").format(sql.Identifier(active)))
    if conds:
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds)
    rows = _run(sql.SQL("SELECT {} AS id, {} AS label FROM {}{} ORDER BY label LIMIT {}").format(
        sql.Identifier(pk), label_expr, t, where, sql.Literal(limit + 1)), params)
    return {
        "labelColumn": label, "truncated": len(rows) > limit,
        "options": [{"id": jsonable(r["id"]), "label": str(r["label"])} for r in rows[:limit]],
    }
