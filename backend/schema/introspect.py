"""Read the structure of `master` (read-only connection)."""
from __future__ import annotations

import hashlib
import json

from gate.operations import normalize_type

from .conn import ro_connection

COLUMNS_SQL = """
SELECT c.relname AS table_name, a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS type, NOT a.attnotnull AS nullable,
       pg_get_expr(d.adbin, d.adrelid) AS "default", col_description(c.oid, a.attnum) AS comment,
       a.attidentity <> '' AS identity
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'master'
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE c.relkind = 'r'
ORDER BY c.relname, a.attnum
"""

CONSTRAINTS_SQL = """
SELECT c.relname AS table_name, con.conname AS name, con.contype AS kind,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'master'
ORDER BY c.relname, con.conname
"""

INDEXES_SQL = """
SELECT tablename AS table_name, indexname AS name, indexdef AS definition
FROM pg_indexes WHERE schemaname = 'master' ORDER BY tablename, indexname
"""

TABLES_SQL = """
SELECT c.relname AS name, obj_description(c.oid) AS comment, GREATEST(c.reltuples, 0)::bigint AS row_estimate
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'master'
WHERE c.relkind = 'r' ORDER BY c.relname
"""

CONSTRAINT_KINDS = {"p": "primary_key", "u": "unique", "f": "foreign_key", "c": "check"}


def snapshot() -> dict:
    """State dict for the gate: {table: {columns, indexes, constraints}}."""
    st: dict = {}
    with ro_connection() as conn:
        for r in conn.execute(TABLES_SQL):
            st[r["name"]] = {"columns": {}, "indexes": set(), "constraints": set()}
        for r in conn.execute(COLUMNS_SQL):
            st[r["table_name"]]["columns"][r["column_name"]] = {
                "type": normalize_type(r["type"]), "nullable": r["nullable"],
            }
        for r in conn.execute(CONSTRAINTS_SQL):
            st[r["table_name"]]["constraints"].add(r["name"])
        for r in conn.execute(INDEXES_SQL):
            st[r["table_name"]]["indexes"].add(r["name"])
    return st


def list_tables() -> list[dict]:
    with ro_connection() as conn:
        return [
            {"name": r["name"], "comment": r["comment"], "rowEstimate": r["row_estimate"]}
            for r in conn.execute(TABLES_SQL)
        ]


def describe_table(name: str) -> dict | None:
    with ro_connection() as conn:
        if not conn.execute(
            "SELECT 1 FROM pg_tables WHERE schemaname = 'master' AND tablename = %s", (name,)
        ).fetchone():
            return None
        t = conn.execute(
            "SELECT obj_description(format('master.%%I', %s::text)::regclass) AS comment", (name,)
        ).fetchone()
        cols = [
            {
                "name": r["column_name"], "type": r["type"], "nullable": r["nullable"],
                "default": r["default"], "comment": r["comment"], "identity": r["identity"],
            }
            for r in conn.execute(COLUMNS_SQL) if r["table_name"] == name
        ]
        cons = [
            {"name": r["name"], "kind": CONSTRAINT_KINDS.get(r["kind"], r["kind"]), "definition": r["definition"]}
            for r in conn.execute(CONSTRAINTS_SQL) if r["table_name"] == name
        ]
        idx = [
            {"name": r["name"], "definition": r["definition"]}
            for r in conn.execute(INDEXES_SQL) if r["table_name"] == name
        ]
    return {"name": name, "comment": t["comment"], "columns": cols, "constraints": cons, "indexes": idx}


def fingerprint(tables: list[str]) -> str:
    """Hash of the structure of `tables`; a missing table hashes as 'absent'.

    Stored with a proposal and compared again at approval time: a difference
    means the schema moved underneath the change, so it is marked stale.
    """
    parts = {}
    with ro_connection() as conn:
        existing = {r["name"] for r in conn.execute(TABLES_SQL)}
    for t in sorted(set(tables)):
        parts[t] = describe_table(t) if t in existing else "absent"
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()
