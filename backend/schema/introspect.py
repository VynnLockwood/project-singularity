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

PK_SQL = """
SELECT c.relname AS table_name, a.attname AS column_name
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'master'
JOIN LATERAL unnest(con.conkey) AS k(attnum) ON true
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE con.contype = 'p'
"""

FK_SQL = """
SELECT c.relname AS from_table, a.attname AS from_column, rc.relname AS to_table, ra.attname AS to_column
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'master'
JOIN pg_class rc ON rc.oid = con.confrelid
JOIN pg_namespace rn ON rn.oid = rc.relnamespace AND rn.nspname = 'master'
JOIN LATERAL unnest(con.conkey, con.confkey) AS k(l, r) ON true
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.l
JOIN pg_attribute ra ON ra.attrelid = rc.oid AND ra.attnum = k.r
WHERE con.contype = 'f'
ORDER BY c.relname, a.attname
"""

UNIQUE_KEYS_SQL = """
SELECT c.relname AS table_name, a.attname AS column_name
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'master'
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = con.conkey[1]
WHERE con.contype IN ('p', 'u') AND cardinality(con.conkey) = 1
"""

CONSTRAINT_KINDS = {"p": "primary_key", "u": "unique", "f": "foreign_key", "c": "check"}


def external_state(state: dict | None = None) -> dict:
    """The tables that already exist, as `scripts.mapping.apply_mapping` names them.

    Same snapshot information, keyed `master.<table>` (the way SQL writes it) and with the unique
    columns a ref may point at: {table: {columns, primary_key: [col], unique: [[col], ...]}}.
    """
    state = snapshot() if state is None else state
    out = {}
    for t, v in state.items():
        out[f"master.{t}"] = {"columns": v["columns"],
                              "primary_key": [v["pk"]] if v["pk"] else [], "unique": []}
    with ro_connection() as conn:
        for r in conn.execute(UNIQUE_KEYS_SQL):
            out[f"master.{r['table_name']}"]["unique"].append([r["column_name"]])
    return out


def snapshot() -> dict:
    """State dict for the gate: {table: {columns, indexes, constraints}}."""
    st: dict = {}
    with ro_connection() as conn:
        for r in conn.execute(TABLES_SQL):
            st[r["name"]] = {"columns": {}, "indexes": set(), "constraints": set(), "pk": None,
                             "comment": r["comment"]}
        for r in conn.execute(COLUMNS_SQL):
            st[r["table_name"]]["columns"][r["column_name"]] = {
                "type": normalize_type(r["type"]), "nullable": r["nullable"],
            }
        for r in conn.execute(CONSTRAINTS_SQL):
            st[r["table_name"]]["constraints"].add(r["name"])
        for r in conn.execute(INDEXES_SQL):
            st[r["table_name"]]["indexes"].add(r["name"])
        for r in conn.execute(PK_SQL):
            st[r["table_name"]]["pk"] = r["column_name"]
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


def graph() -> dict:
    """Everything the schema diagram needs in one read: tables with columns, and foreign-key edges."""
    with ro_connection() as conn:
        tables = {
            r["name"]: {"name": r["name"], "comment": r["comment"], "rowEstimate": r["row_estimate"], "columns": []}
            for r in conn.execute(TABLES_SQL)
        }
        pks = {(r["table_name"], r["column_name"]) for r in conn.execute(PK_SQL)}
        for r in conn.execute(COLUMNS_SQL):
            tables[r["table_name"]]["columns"].append({
                "name": r["column_name"], "type": r["type"], "nullable": r["nullable"],
                "primaryKey": (r["table_name"], r["column_name"]) in pks,
            })
        relations = [
            {"from": {"table": r["from_table"], "column": r["from_column"]},
             "to": {"table": r["to_table"], "column": r["to_column"]}}
            for r in conn.execute(FK_SQL)
        ]
    return {"tables": list(tables.values()), "relations": relations}


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
