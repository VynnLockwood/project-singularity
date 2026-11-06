"""Apply validated operations to `master`: one transaction, all or nothing.

This is the only code that writes to `master`. It also captures what is needed
to undo each operation (spec section 2, "Undo"), so a revert is just another
change through the gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator

import psycopg
from psycopg import sql

from gate import sqlgen
from gate.conventions import Conventions
from gate.state import fk_name, index_name, pk_of, role_of, unique_name

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


def _before_rows(conn, table: str, where: dict, cols: list[str], max_rows: int, pk: str) -> list[dict]:
    """Rows about to change, each with its primary key under the key "pk"."""
    q = sql.SQL("SELECT {} FROM {} WHERE {} LIMIT {} FOR UPDATE").format(
        sql.SQL(", ").join([sql.SQL("{} AS pk").format(sql.Identifier(pk)), *(sql.Identifier(c) for c in cols)]),
        sqlgen.tbl(table),
        _where_sql(where),
        sql.Literal(max_rows + 1),
    )
    rows = conn.execute(q).fetchall()
    if len(rows) > max_rows:
        raise ApplyError(f"operation would touch more than {max_rows} rows")
    return rows


def _group_updates(table: str, rows: list[dict], cols: list[str], pk: str) -> list[dict]:
    """Restore ops: rows with identical old values share one update (pk IN [...])."""
    groups: dict[str, dict] = {}
    for r in rows:
        values = {c: jsonable(r[c]) for c in cols}
        key = repr(sorted(values.items(), key=lambda kv: kv[0]))
        groups.setdefault(key, {"values": values, "ids": []})["ids"].append(r["pk"])
    return [
        {"op": "update_rows", "table": table, "where": {pk: g["ids"]}, "set": g["values"]}
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


def _table_comment(conn, table: str) -> str:
    row = conn.execute(
        "SELECT obj_description(format('master.%%I', %s::text)::regclass) AS c", (table,)
    ).fetchone()
    return (row["c"] if row else None) or ""


TMP = sql.Identifier("_bulk_src")
ROWNO = sql.Identifier("__row")
TEXTY = ("text", "character varying")


def _cast(col: sql.Composable, pg_type: str) -> sql.Composable:
    """tmp column -> target type; empty text means NULL for every non-text type."""
    if pg_type.split("(")[0] in TEXTY:
        return sql.SQL("{}::{}").format(col, sql.SQL(pg_type))
    return sql.SQL("NULLIF({}, '')::{}").format(col, sql.SQL(pg_type))


def _bulk_insert(conn, op: dict, before: dict, actor: str | None, row_source) -> list[dict] | None:
    """COPY the rows into a temp table of text columns, then INSERT ... SELECT into the target.

    Temp-table columns are exactly op["columns"], in that order, and row_source(op) must yield
    tuples (str | None) in that order. Columns named as a ref's source_column only feed the join;
    every other column is inserted directly into the column of the same name.
    """
    t = op["table"]
    if row_source is None:
        raise ApplyError("bulk_insert needs a row source (it only runs from script intake)")
    pk = pk_of(before, t)
    refs = op.get("refs", [])
    carriers = {r["source_column"] for r in refs}
    direct = [c for c in op["columns"] if c not in carriers]
    I = sql.Identifier

    conn.execute(sql.SQL("CREATE TEMP TABLE {} ({}, {} bigserial) ON COMMIT DROP").format(
        TMP, sql.SQL(", ").join(sql.SQL("{} text").format(I(c)) for c in op["columns"]), ROWNO))
    width = len(op["columns"])
    with conn.cursor() as cur:
        with cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(TMP, sql.SQL(", ").join(I(c) for c in op["columns"]))) as cp:
            for row in row_source(op):
                if len(row) != width:
                    raise ApplyError(f"bulk_insert source row has {len(row)} values, expected {width}")
                cp.write_row(row)
    loaded = conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(TMP)).fetchone()["n"]
    if loaded != op["expected_rows"]:
        raise ApplyError(f"bulk_insert into {t}: source has {loaded} rows, expected {op['expected_rows']}")

    targets: list[sql.Composable] = []
    names = [*direct, *(r["column"] for r in refs)]
    values: list[sql.Composable] = []
    joins: list[sql.Composable] = []
    for c in direct:
        ty = _col_type(conn, t, c)
        if ty is None:
            raise ApplyError(f"column {t}.{c} does not exist")
        targets.append(I(c))
        values.append(_cast(sql.SQL("s.{}").format(I(c)), ty))
    for i, r in enumerate(refs):
        pt, pkey = r["parent_table"], r["parent_key"]
        ppk = pk_of(before, pt)
        kty = _col_type(conn, pt, pkey)
        if ppk is None or kty is None:
            raise ApplyError(f"cannot resolve {pt}.{pkey}")
        key = _cast(sql.SQL("s.{}").format(I(r["source_column"])), kty)
        alias = sql.Identifier(f"p{i}")
        on = sql.SQL("{}.{} = {}").format(alias, I(pkey), key)
        orphans = conn.execute(
            sql.SQL("SELECT count(*) AS n, min(s.{src}) AS example FROM {tmp} s LEFT JOIN {pt} {a} ON {on} "
                    "WHERE s.{src} IS NOT NULL AND s.{src} <> '' AND {a}.{ppk} IS NULL").format(
                src=I(r["source_column"]), tmp=TMP, pt=sqlgen.tbl(pt), a=alias, on=on, ppk=I(ppk))
        ).fetchone()
        if orphans["n"]:
            raise ApplyError(
                f"bulk_insert into {t}: {orphans['n']} row(s) refer to a missing {pt}.{pkey} (for example {orphans['example']!r})")
        targets.append(I(r["column"]))
        values.append(sql.SQL("{}.{}").format(alias, I(ppk)))
        joins.append(sql.SQL("LEFT JOIN {} {} ON {}").format(sqlgen.tbl(pt), alias, on))
    by = role_of(before, t, "created_by")
    if actor and by and by not in names:
        targets.append(I(by))
        values.append(sql.Literal(actor))

    stmt = sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {} s {} ORDER BY s.{}{}").format(
        sqlgen.tbl(t), sql.SQL(", ").join(targets), sql.SQL(", ").join(values), TMP, sql.SQL(" ").join(joins), ROWNO,
        sql.SQL(" RETURNING {}").format(I(pk)) if pk else sql.SQL(""))
    cur = conn.execute(stmt)
    ids = [r[pk] for r in cur.fetchall()] if pk else []
    if cur.rowcount != op["expected_rows"]:
        raise ApplyError(f"bulk_insert into {t}: inserted {cur.rowcount} rows, expected {op['expected_rows']}")
    conn.execute(sql.SQL("DROP TABLE {}").format(TMP))

    if pk:
        # explicit ids (identity BY DEFAULT / serial) leave the sequence behind: move it up to max(pk)
        seq = conn.execute("SELECT pg_get_serial_sequence(format('master.%%I', %s::text), %s) AS s", (t, pk)).fetchone()["s"]
        if seq:
            conn.execute(
                sql.SQL("SELECT setval(%s::regclass, m) FROM (SELECT max({}) AS m FROM {}) x "
                        "WHERE m IS NOT NULL AND m > coalesce(pg_sequence_last_value(%s::regclass), 0)").format(
                    I(pk), sqlgen.tbl(t)), (seq, seq))
    if ids and role_of(before, t, "is_active"):
        return [{"op": "deactivate_rows", "table": t, "where": {pk: ids}}]
    return None


def _run_op(conn, op: dict, before: dict, stmts, max_rows: int, actor=None, row_source=None):
    """Execute one operation; return its reverse op(s) (a list) or None if irreversible."""
    k = op["op"]
    t = op.get("table")
    if k == "bulk_insert":
        return _bulk_insert(conn, op, before, actor, row_source)

    # --- capture what we need before changing anything
    prev_type = prev_comment = rows_before = None
    if k == "alter_column_type":
        prev_type = _col_type(conn, t, op["column"])
    elif k == "set_column_comment":
        prev_comment = _col_comment(conn, t, op["column"])
    elif k == "set_table_comment":
        prev_comment = _table_comment(conn, t)
    elif k == "update_rows" and pk_of(before, t):
        rows_before = _before_rows(conn, t, op["where"], list(op["set"]), max_rows, pk_of(before, t))
    elif k == "deactivate_rows" and pk_of(before, t):
        active_col = role_of(before, t, "is_active")
        if active_col is None:
            raise ApplyError(f"table {t!r} has no column with the is_active role")
        rows_before = _before_rows(conn, t, op["where"], [active_col], max_rows, pk_of(before, t))

    # --- run
    inserted_ids: list = []
    rowcount = None
    for s in stmts:
        cur = conn.execute(s.sql)
        if s.returning_id:
            inserted_ids.append(cur.fetchone()[pk_of(before, t)])
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
    if k == "set_table_comment":
        return [{"op": "set_table_comment", "table": t, "comment": prev_comment}]
    if k == "add_check":
        return [{"op": "drop_constraint", "table": t, "name": op["name"], "reason": why}]
    if k == "add_index":
        return [{"op": "drop_index", "name": op.get("name") or index_name(t, op["columns"]), "reason": why}]
    if k == "add_unique":
        return [{"op": "drop_constraint", "table": t, "name": op.get("name") or unique_name(t, op["columns"]), "reason": why}]
    if k == "add_foreign_key":
        return [{"op": "drop_constraint", "table": t, "name": op.get("name") or fk_name(t, op["column"]), "reason": why}]
    if k in ("drop_column", "drop_table", "drop_index", "drop_constraint"):
        return None
    if k == "insert_rows":
        if inserted_ids and role_of(before, t, "is_active"):
            return [{"op": "deactivate_rows", "table": t, "where": {pk_of(before, t): inserted_ids}}]
        return None
    if k == "update_rows":
        return _group_updates(t, rows_before, list(op["set"]), pk_of(before, t)) if rows_before is not None else None
    if k == "deactivate_rows":
        if rows_before is None:
            return None
        active_col = role_of(before, t, "is_active")
        ids = [r["pk"] for r in rows_before if r[active_col]]
        return [{"op": "update_rows", "table": t, "where": {pk_of(before, t): ids}, "set": {active_col: True}}] if ids else []
    return None


def execute(ops: list[dict], state: dict, actor: str | None, *, max_rows: int = 1000,
            rules: list[dict], row_source: Callable[[dict], Iterator[tuple]] | None = None,
            dry_run: bool = False) -> ExecResult:
    """Run `ops` in one transaction as the read/write role. Raises ApplyError on any failure.

    row_source(op) feeds bulk_insert ops (see _bulk_insert). dry_run runs everything,
    then rolls back, and returns the same ExecResult a real run would.
    """
    reverse: list[list[dict] | None] = []
    count = 0
    try:
        with rw_connection() as conn:
            conn.execute("SET LOCAL lock_timeout = '5s'")
            conn.execute("SET LOCAL statement_timeout = '120s'")
            for op, before, stmts in sqlgen.plan(ops, state, actor, Conventions.from_rules(rules)):
                reverse.append(_run_op(conn, op, before, stmts, max_rows, actor, row_source))
                count += len(stmts) or 1
            if dry_run:
                conn.rollback()
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
