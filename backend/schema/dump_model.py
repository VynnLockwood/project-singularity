"""Read the full structure of one schema from the Postgres catalogs.

Every catalog query takes the schema as a bound parameter; nothing user-supplied
is formatted into SQL. Only the requested schema is read, so Singularity's own
tables never appear.
"""
from __future__ import annotations

import heapq


class ExportError(Exception):
    """An export that cannot be produced faithfully. `str(exc)` is the message."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def allowed_schemas() -> list[str]:
    return ["master"]


_TABLES = """
SELECT c.oid, c.relname AS name, c.relkind, c.relispartition, obj_description(c.oid, 'pg_class') AS comment
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relkind IN ('r', 'p')
ORDER BY c.relname
"""

_COLUMNS = """
SELECT a.attrelid AS rel, a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type,
       NOT a.attnotnull AS nullable, pg_get_expr(d.adbin, d.adrelid) AS "default",
       a.attidentity AS identity, a.attgenerated AS generated, col_description(a.attrelid, a.attnum) AS comment
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
"""

_CONSTRAINTS = """
SELECT k.conrelid AS rel, k.conname AS name, k.contype AS kind,
       pg_get_constraintdef(k.oid) AS definition
FROM pg_constraint k
JOIN pg_class c ON c.oid = k.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND k.contype IN ('p', 'u', 'c', 'f', 'x')
ORDER BY k.conrelid, k.conname
"""

_INDEXES = """
SELECT i.indrelid AS rel, ic.relname AS name, pg_get_indexdef(i.indexrelid) AS definition
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relkind IN ('r', 'p')
  AND NOT EXISTS (
    SELECT 1 FROM pg_constraint k
    WHERE k.conindid = i.indexrelid AND k.conrelid = i.indrelid AND k.contype IN ('p', 'u', 'x')
  )
ORDER BY i.indrelid, ic.relname
"""

_REFERENCES = """
SELECT k.conrelid AS rel, k.confrelid AS target
FROM pg_constraint k
JOIN pg_class c ON c.oid = k.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND k.contype = 'f'
"""

def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


_IDENTITY = {"a": "always", "d": "by default"}


def read_model(conn, schema: str) -> dict:
    def fetch(sql: str) -> list[dict]:
        return conn.execute(sql, (schema,)).fetchall()

    rows = fetch(_TABLES)
    for t in rows:
        if t["relkind"] == "p" or t["relispartition"]:
            raise ExportError(
                f'Table {_q(t["name"])} is {"a partition" if t["relispartition"] else "partitioned"}; '
                "partitioned tables cannot be exported.", 422)

    tables = {t["oid"]: {"name": t["name"], "comment": t["comment"], "columns": [],
                         "constraints": [], "indexes": [], "references": []}
              for t in rows}

    for c in fetch(_COLUMNS):
        t = tables.get(c["rel"])
        if t is None:
            continue
        if c["generated"]:
            raise ExportError(
                f'Column {_q(t["name"])}.{_q(c["name"])} is a generated column; '
                "generated columns cannot be exported.", 422)
        default = c["default"]
        if default is not None and "nextval(" in default:
            raise ExportError(
                f'Column {_q(t["name"])}.{_q(c["name"])} uses a sequence default ({default}); '
                "sequence-backed defaults cannot be exported. Use an identity column instead.",
                422,
            )
        t["columns"].append({
            "name": c["name"], "type": c["type"], "nullable": bool(c["nullable"]),
            "default": default, "identity": _IDENTITY.get(c["identity"]), "comment": c["comment"],
        })

    for k in fetch(_CONSTRAINTS):
        if k["rel"] in tables:
            if k["kind"] == "x":
                raise ExportError(
                    f'Table {_q(tables[k["rel"]]["name"])} has exclusion constraint {_q(k["name"])}; '
                    "exclusion constraints cannot be exported.", 422)
            tables[k["rel"]]["constraints"].append(
                {"name": k["name"], "kind": k["kind"], "definition": k["definition"]})

    for i in fetch(_INDEXES):
        if i["rel"] in tables:
            tables[i["rel"]]["indexes"].append({"name": i["name"], "definition": i["definition"]})

    kept = {oid: t for oid, t in tables.items() if t["columns"]}
    for r in fetch(_REFERENCES):
        src, dst = kept.get(r["rel"]), kept.get(r["target"])
        if src and dst and dst["name"] != src["name"] and dst["name"] not in src["references"]:
            src["references"].append(dst["name"])

    out = sorted(kept.values(), key=lambda t: t["name"])
    for t in out:
        t["references"].sort()
    return {"tables": out}


def table_order(model: dict) -> list[str]:
    """Parents first, alphabetical tie-break; tables stuck in a cycle go last, alphabetical."""
    names = {t["name"] for t in model["tables"]}
    deps = {t["name"]: {r for r in t.get("references", []) if r in names and r != t["name"]}
            for t in model["tables"]}
    children: dict[str, list[str]] = {n: [] for n in names}
    for n, ds in deps.items():
        for d in ds:
            children[d].append(n)
    waiting = {n: len(ds) for n, ds in deps.items()}
    heap = [n for n, w in waiting.items() if w == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        n = heapq.heappop(heap)
        order.append(n)
        for ch in children[n]:
            waiting[ch] -= 1
            if waiting[ch] == 0:
                heapq.heappush(heap, ch)
    done = set(order)
    return order + sorted(names - done)
