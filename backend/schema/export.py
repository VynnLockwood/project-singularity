"""Render the designed schema graph as Postgres DDL.

Built in-app rather than shelling out to pg_dump, which would dump Singularity's
own database (internal tables, roles) instead of the designed schema.
"""

from typing import Any


def _ident(name: str) -> str:
    """Quote an identifier, doubling any embedded quotes."""
    return '"' + str(name).replace('"', '""') + '"'


def to_sql(graph: dict[str, Any]) -> str:
    """CREATE TABLE per table, then one FK ALTER per relation. Every identifier double-quoted."""
    out: list[str] = []

    for table in graph.get("tables") or []:
        cols: list[str] = []
        pk: list[str] = []
        for col in table.get("columns") or []:
            parts = [_ident(col["name"]), col["type"]]
            if not col.get("nullable", True):
                parts.append("NOT NULL")
            cols.append(" ".join(parts))
            if col.get("primaryKey"):
                pk.append(_ident(col["name"]))
        if pk:
            cols.append(f"PRIMARY KEY ({', '.join(pk)})")
        out.append(
            f"CREATE TABLE {_ident(table['name'])} (\n  "
            + ",\n  ".join(cols)
            + "\n);"
        )

    for rel in graph.get("relations") or []:
        src, dst = rel["from"], rel["to"]
        out.append(
            f"ALTER TABLE {_ident(src['table'])} "
            f"ADD FOREIGN KEY ({_ident(src['column'])}) "
            f"REFERENCES {_ident(dst['table'])} ({_ident(dst['column'])});"
        )

    return "\n".join(out) + "\n" if out else ""
