"""Read the structure of a managed schema (default `master`) with the read-only connection."""
from __future__ import annotations

import hashlib
import json

from gate.operations import normalize_type
from gate.state import State

from .conn import ro_connection


class UnknownSchema(Exception):
    """The schema is not one of the managed schemas."""


MANAGED_SQL = """
SELECT n.nspname AS name, obj_description(n.oid, 'pg_namespace') AS comment,
       (SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid AND c.relkind IN ('r', 'p')) AS tables
FROM pg_namespace n
WHERE n.nspname NOT IN ('singularity', 'public', 'information_schema') AND n.nspname NOT LIKE 'pg\\_%'
  AND (n.nspname = 'master'
       OR n.nspowner = (SELECT oid FROM pg_roles WHERE rolname = 'singularity_master_rw'))
ORDER BY (n.nspname = 'master') DESC, n.nspname
"""


def managed_schemas() -> list[dict]:
    """Managed schemas: `master` first, then the others owned by the rw role, sorted."""
    with ro_connection() as conn:
        return [{"name": r["name"], "tables": int(r["tables"]), "comment": r["comment"]}
                for r in conn.execute(MANAGED_SQL)]


def require_managed(schema: str) -> str:
    if not isinstance(schema, str) or schema not in {m["name"] for m in managed_schemas()}:
        raise UnknownSchema(f"Unknown schema {schema!r}.")
    return schema

COLUMNS_SQL = """
SELECT c.relname AS table_name, a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS type, NOT a.attnotnull AS nullable,
       pg_get_expr(d.adbin, d.adrelid) AS "default", col_description(c.oid, a.attnum) AS comment,
       a.attidentity <> '' AS identity
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = %(schema)s
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
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = %(schema)s
ORDER BY c.relname, con.conname
"""

INDEXES_SQL = """
SELECT tablename AS table_name, indexname AS name, indexdef AS definition
FROM pg_indexes WHERE schemaname = %(schema)s ORDER BY tablename, indexname
"""

TABLES_SQL = """
SELECT c.relname AS name, obj_description(c.oid) AS comment, GREATEST(c.reltuples, 0)::bigint AS row_estimate
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = %(schema)s
WHERE c.relkind = 'r' ORDER BY c.relname
"""

PK_SQL = """
SELECT c.relname AS table_name, a.attname AS column_name
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = %(schema)s
JOIN LATERAL unnest(con.conkey) AS k(attnum) ON true
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE con.contype = 'p'
"""

FK_SQL = """
SELECT c.relname AS from_table, a.attname AS from_column, rc.relname AS to_table, ra.attname AS to_column
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = %(schema)s
JOIN pg_class rc ON rc.oid = con.confrelid
JOIN pg_namespace rn ON rn.oid = rc.relnamespace AND rn.nspname = %(schema)s
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
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = %(schema)s
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = con.conkey[1]
WHERE con.contype IN ('p', 'u') AND cardinality(con.conkey) = 1
"""

KEYS_SQL = """
SELECT c.relname AS table_name, con.conname AS name, array_agg(a.attname ORDER BY k.ord) AS cols
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = %(schema)s
JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE con.contype IN ('p', 'u')
GROUP BY c.relname, con.conname
"""

UNIQUE_INDEXES_SQL = """
SELECT c.relname AS table_name, i.relname AS name, array_agg(a.attname ORDER BY k.ord) AS cols
FROM pg_index x
JOIN pg_class c ON c.oid = x.indrelid
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = %(schema)s
JOIN LATERAL unnest(x.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) ON true
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE x.indisunique AND NOT x.indisprimary AND x.indexprs IS NULL AND x.indpred IS NULL
  AND NOT EXISTS (SELECT 1 FROM pg_constraint con WHERE con.conindid = x.indexrelid)
GROUP BY c.relname, i.relname
"""

ENUMS_SQL = """
SELECT t.typname AS name, e.enumlabel AS label
FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace AND n.nspname = %(schema)s
JOIN pg_enum e ON e.enumtypid = t.oid
ORDER BY t.typname, e.enumsortorder
"""

VIEWS_SQL = """
SELECT c.relname AS name, pg_get_viewdef(c.oid) AS query, obj_description(c.oid) AS comment
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = %(schema)s
WHERE c.relkind = 'v' ORDER BY c.relname
"""

VIEW_TABLES_SQL = """
SELECT DISTINCT v.relname AS view_name, t.relname AS table_name
FROM pg_rewrite r
JOIN pg_class v ON v.oid = r.ev_class AND v.relkind = 'v'
JOIN pg_namespace n ON n.oid = v.relnamespace AND n.nspname = %(schema)s
JOIN pg_depend d ON d.objid = r.oid AND d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass
JOIN pg_class t ON t.oid = d.refobjid AND t.oid <> v.oid
JOIN pg_namespace tn ON tn.oid = t.relnamespace AND tn.nspname = %(schema)s
"""

CONSTRAINT_KINDS = {"p": "primary_key", "u": "unique", "f": "foreign_key", "c": "check"}


def external_state(state: dict | None = None, schema: str = "master") -> dict:
    """The tables that already exist, as `scripts.mapping.apply_mapping` names them.

    Same snapshot information, keyed `<schema>.<table>` (the way SQL writes it) and with the unique
    columns a ref may point at: {table: {columns, primary_key: [col], unique: [[col], ...]}}.
    """
    require_managed(schema)
    state = snapshot(schema) if state is None else state
    out = {}
    for t, v in state.items():
        out[f"{schema}.{t}"] = {"columns": v["columns"],
                                "primary_key": [v["pk"]] if v["pk"] else [], "unique": []}
    with ro_connection() as conn:
        for r in conn.execute(UNIQUE_KEYS_SQL, {"schema": schema}):
            out[f"{schema}.{r['table_name']}"]["unique"].append([r["column_name"]])
    return out


def snapshot(schema: str = "master") -> State:
    """State for the gate: {table: {columns, indexes, constraints, pk, comment, keys, key_names, links}}
    with `.views` and `.enums` for the schema's views and enum types."""
    require_managed(schema)
    st = State()
    with ro_connection() as conn:
        for r in conn.execute(TABLES_SQL, {"schema": schema}):
            st[r["name"]] = {"columns": {}, "indexes": set(), "constraints": set(), "pk": None,
                             "comment": r["comment"]}
        for r in conn.execute(COLUMNS_SQL, {"schema": schema}):
            st[r["table_name"]]["columns"][r["column_name"]] = {
                "type": normalize_type(r["type"]), "nullable": r["nullable"], "default": r["default"],
            }
        for r in conn.execute(CONSTRAINTS_SQL, {"schema": schema}):
            st[r["table_name"]]["constraints"].add(r["name"])
        for r in conn.execute(INDEXES_SQL, {"schema": schema}):
            st[r["table_name"]]["indexes"].add(r["name"])
        for r in conn.execute(PK_SQL, {"schema": schema}):
            st[r["table_name"]]["pk"] = r["column_name"]
        for t in st.values():
            t["keys"], t["key_names"], t["links"] = set(), {}, set()
        for sql_ in (KEYS_SQL, UNIQUE_INDEXES_SQL):
            for r in conn.execute(sql_, {"schema": schema}):
                key = frozenset(r["cols"])
                st[r["table_name"]]["keys"].add(key)
                st[r["table_name"]]["key_names"][r["name"]] = key
        for r in conn.execute(FK_SQL, {"schema": schema}):
            a, b = r["from_table"], r["to_table"]
            if a != b:
                st[a]["links"].add(b)
                st[b]["links"].add(a)
        for r in conn.execute(ENUMS_SQL, {"schema": schema}):
            st.enums.setdefault(r["name"], []).append(r["label"])
        for t in st.values():       # format_type qualifies enum names that are not on the search path
            for c in t["columns"].values():
                bare = c["type"].removeprefix(f"{schema}.")
                if bare in st.enums:
                    c["type"] = bare
        for r in conn.execute(VIEWS_SQL, {"schema": schema}):
            st.views[r["name"]] = {"query": r["query"], "tables": set()}
        for r in conn.execute(VIEW_TABLES_SQL, {"schema": schema}):
            st.views[r["view_name"]]["tables"].add(r["table_name"])
    return st


def count_where(table: str, where: dict, schema: str = "master") -> int:
    """How many rows of `table` match `where` (same grammar as update_rows). Read-only."""
    from psycopg import sql
    from gate import sqlgen
    require_managed(schema)
    q = sql.SQL("SELECT count(*) AS n FROM {} WHERE {}").format(sqlgen.tbl(table, schema), sqlgen._where(where))
    with ro_connection() as conn:
        return int(conn.execute(q).fetchone()["n"])


def list_tables(schema: str = "master") -> list[dict]:
    require_managed(schema)
    with ro_connection() as conn:
        return [
            {"name": r["name"], "comment": r["comment"], "rowEstimate": r["row_estimate"]}
            for r in conn.execute(TABLES_SQL, {"schema": schema})
        ]


def describe_table(name: str, schema: str = "master") -> dict | None:
    require_managed(schema)
    with ro_connection() as conn:
        if not conn.execute(
            "SELECT 1 FROM pg_tables WHERE schemaname = %s AND tablename = %s", (schema, name)
        ).fetchone():
            return None
        t = conn.execute(
            "SELECT obj_description(format('%%I.%%I', %s::text, %s::text)::regclass) AS comment", (schema, name)
        ).fetchone()
        cols = [
            {
                "name": r["column_name"], "type": r["type"], "nullable": r["nullable"],
                "default": r["default"], "comment": r["comment"], "identity": r["identity"],
            }
            for r in conn.execute(COLUMNS_SQL, {"schema": schema}) if r["table_name"] == name
        ]
        cons = [
            {"name": r["name"], "kind": CONSTRAINT_KINDS.get(r["kind"], r["kind"]), "definition": r["definition"]}
            for r in conn.execute(CONSTRAINTS_SQL, {"schema": schema}) if r["table_name"] == name
        ]
        idx = [
            {"name": r["name"], "definition": r["definition"]}
            for r in conn.execute(INDEXES_SQL, {"schema": schema}) if r["table_name"] == name
        ]
    return {"name": name, "comment": t["comment"], "columns": cols, "constraints": cons, "indexes": idx}


def graph(schema: str = "master") -> dict:
    """Everything the schema diagram needs in one read: tables with columns, and foreign-key edges."""
    require_managed(schema)
    with ro_connection() as conn:
        tables = {
            r["name"]: {"name": r["name"], "comment": r["comment"], "rowEstimate": r["row_estimate"], "columns": []}
            for r in conn.execute(TABLES_SQL, {"schema": schema})
        }
        pks = {(r["table_name"], r["column_name"]) for r in conn.execute(PK_SQL, {"schema": schema})}
        for r in conn.execute(COLUMNS_SQL, {"schema": schema}):
            tables[r["table_name"]]["columns"].append({
                "name": r["column_name"], "type": r["type"], "nullable": r["nullable"],
                "primaryKey": (r["table_name"], r["column_name"]) in pks,
            })
        relations = [
            {"from": {"table": r["from_table"], "column": r["from_column"]},
             "to": {"table": r["to_table"], "column": r["to_column"]}}
            for r in conn.execute(FK_SQL, {"schema": schema})
        ]
    return {"tables": list(tables.values()), "relations": relations}


def fingerprint(tables: list[str], schema: str = "master") -> str:
    """Hash of the structure of `tables`; a missing table hashes as 'absent'.

    Stored with a proposal and compared again at approval time: a difference
    means the schema moved underneath the change, so it is marked stale.
    """
    require_managed(schema)
    parts = {}
    with ro_connection() as conn:
        existing = {r["name"] for r in conn.execute(TABLES_SQL, {"schema": schema})}
    for t in sorted(set(tables)):
        parts[t] = describe_table(t, schema) if t in existing else "absent"
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()
