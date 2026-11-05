"""Apply validated operations to `master`: one transaction, all or nothing.

This is the only code that writes to `master`. It also captures what is needed
to undo each operation (spec section 2, "Undo"), so a revert is just another
change through the gate.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg import sql

from gate import sqlgen
from gate.state import fk_name, has_column, index_name, unique_name

from .conn import rw_connection
from .util import jsonable, pg_type_to_allowed


class ApplyError(Exception):
    pass


@dataclass
class ExecResult:
    reverse_ops: list[dict] | None   # None = cannot be undone automatically
    statements: int


def _where_sql(where: dict):
    return sqlgen._where(where)


def _before_rows(conn, table: str, where: dict, cols: list[str], max_rows: int) -> list[dict]:
    q = sql.SQL("SELECT {} FROM {} WHERE {} LIMIT {} FOR UPDATE").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in ["id", *cols]),
        sqlgen.tbl(table),
        _where_sql(where),
        sql.Literal(max_rows + 1),
    )
    rows = conn.execute(q).fetchall()
    if len(rows) > max_rows:
        raise ApplyError(f"operation would touch more than {max_rows} rows")
    return rows


def _group_updates(table: str, rows: list[dict], cols: list[str]) -> list[dict]:
    """Restore ops: rows with identical old values share one update (id IN [...])."""
    groups: dict[str, dict] = {}
    for r in rows:
        values = {c: jsonable(r[c]) for c in cols}
        key = repr(sorted(values.items(), key=lambda kv: kv[0]))
        groups.setdefault(key, {"values": values, "ids": []})["ids"].append(r["id"])
    return [
        {"op": "update_rows", "table": table, "where": {"id": g["ids"]}, "set": g["values"]}
        for g in groups.values()
    ]


def _col_type(conn, table: str, column: str) -> str | None:
    row = conn.execute(
        "SELECT format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a "
        "WHERE a.attrelid = format('master.%%I', %s::text)::regclass AND a.attname = %s AND NOT a.attisdropped",
        (table, column),
    ).fetchone()
    return row["t"] if row else None


def _col_comment(conn, table: str, column: str) -> str:
    row = conn.execute(
        "SELECT col_description(a.attrelid, a.attnum) AS c FROM pg_attribute a "
        "WHERE a.attrelid = format('master.%%I', %s::text)::regclass AND a.attname = %s",
        (table, column),
    ).fetchone()
    return (row["c"] if row else None) or ""


def _run_op(conn, op: dict, before: dict, stmts, max_rows: int):
    """Execute one operation; return its reverse op(s) (a list) or None if irreversible."""
    k = op["op"]
    t = op.get("table")

    # --- capture what we need before changing anything
    prev_type = prev_comment = rows_before = None
    if k == "alter_column_type":
        prev_type = _col_type(conn, t, op["column"])
    elif k == "set_column_comment":
        prev_comment = _col_comment(conn, t, op["column"])
    elif k == "update_rows" and has_column(before, t, "id"):
        rows_before = _before_rows(conn, t, op["where"], list(op["set"]), max_rows)
    elif k == "deactivate_rows" and has_column(before, t, "id"):
        rows_before = _before_rows(conn, t, op["where"], ["isActive"], max_rows)

    # --- run
    inserted_ids: list = []
    rowcount = None
    for s in stmts:
        cur = conn.execute(s.sql)
        if s.returning_id:
            inserted_ids.append(cur.fetchone()["id"])
        if k in ("update_rows", "deactivate_rows"):
            rowcount = cur.rowcount
    if rowcount is not None and rowcount > max_rows:
        raise ApplyError(f"operation touched {rowcount} rows (limit {max_rows})")

    # --- reverse
    why = "revert"
    if k == "create_table":
        return [{"op": "drop_table", "table": op["name"], "reason": why}]
    if k == "add_column":
        return [{"op": "drop_column", "table": t, "column": op["column"]["name"], "reason": why}]
    if k == "rename_table":
        return [{"op": "rename_table", "table": op["new_name"], "new_name": t}]
    if k == "rename_column":
        return [{"op": "rename_column", "table": t, "column": op["new_name"], "new_name": op["column"]}]
    if k == "alter_column_type":
        allowed = pg_type_to_allowed(prev_type or "")
        return [{"op": "alter_column_type", "table": t, "column": op["column"], "type": allowed}] if allowed else None
    if k == "set_column_comment":
        return [{"op": "set_column_comment", "table": t, "column": op["column"], "comment": prev_comment}]
    if k == "add_index":
        return [{"op": "drop_index", "name": op.get("name") or index_name(t, op["columns"]), "reason": why}]
    if k == "add_unique":
        return [{"op": "drop_constraint", "table": t, "name": op.get("name") or unique_name(t, op["columns"]), "reason": why}]
    if k == "add_foreign_key":
        return [{"op": "drop_constraint", "table": t, "name": op.get("name") or fk_name(t, op["column"]), "reason": why}]
    if k in ("drop_column", "drop_table", "drop_index", "drop_constraint"):
        return None
    if k == "insert_rows":
        if inserted_ids and has_column(before, t, "isActive"):
            return [{"op": "deactivate_rows", "table": t, "where": {"id": inserted_ids}}]
        return None
    if k == "update_rows":
        return _group_updates(t, rows_before, list(op["set"])) if rows_before is not None else None
    if k == "deactivate_rows":
        if rows_before is None:
            return None
        ids = [r["id"] for r in rows_before if r["isActive"]]
        return [{"op": "update_rows", "table": t, "where": {"id": ids}, "set": {"isActive": True}}] if ids else []
    return None


def execute(ops: list[dict], state: dict, actor: str | None, *, max_rows: int = 1000) -> ExecResult:
    """Run `ops` in one transaction as the read/write role. Raises ApplyError on any failure."""
    reverse: list[list[dict] | None] = []
    count = 0
    try:
        with rw_connection() as conn:
            conn.execute("SET LOCAL lock_timeout = '5s'")
            conn.execute("SET LOCAL statement_timeout = '120s'")
            for op, before, stmts in sqlgen.plan(ops, state, actor):
                reverse.append(_run_op(conn, op, before, stmts, max_rows))
                count += len(stmts)
    except ApplyError:
        raise
    except psycopg.Error as exc:
        msg = exc.diag.message_primary or str(exc)
        detail = f" ({exc.diag.message_detail})" if exc.diag.message_detail else ""
        raise ApplyError(f"{msg}{detail}") from exc

    if any(r is None for r in reverse):
        return ExecResult(None, count)
    flat = [op for group in reversed(reverse) for op in group]
    return ExecResult(flat or None, count)
