"""Render a `dump_model.read_model` dict as Postgres DDL.

Built in-app rather than shelling out to pg_dump, which would dump Singularity's
own database (internal tables, roles) instead of the designed schema. Every
renderer is pure (no DB). Identifiers are always double-quoted with embedded
quotes doubled; the only free text that enters SQL is a comment, via `_lit`.
"""
from __future__ import annotations

import datetime

from .conn import ro_connection
from .dump_model import ExportError, allowed_schemas, read_model, table_order


def _ident(name: str) -> str:
    """Quote an identifier, doubling any embedded quotes."""
    return '"' + str(name).replace('"', '""') + '"'


def _lit(text: str) -> str:
    """A standard SQL string literal: single quotes doubled."""
    return "'" + str(text).replace("'", "''") + "'"


def _qualified(schema: str, table: str) -> str:
    return f"{_ident(schema)}.{_ident(table)}"


def _ordered(model: dict) -> list[dict]:
    by_name = {t["name"]: t for t in model["tables"]}
    return [by_name[n] for n in table_order(model)]


def render_header(schema: str, include_data: bool, counts: dict[str, int], today: datetime.date) -> str:
    lines = [
        "-- Singularity export",
        f"-- schema: {schema}",
        f"-- date: {today.isoformat()}",
        f"-- data: {'included' if include_data else 'not included'}",
    ]
    lines += [f"-- {name}: {n} rows" for name, n in counts.items()]
    return "\n".join(line.replace("\n", " ") for line in lines) + "\n"


def _column_line(c: dict) -> str:
    parts = [_ident(c["name"]), c["type"]]
    if c.get("identity"):
        parts.append(f"GENERATED {c['identity'].upper()} AS IDENTITY")
    if c.get("default") is not None:
        parts.append(f"DEFAULT {c['default']}")
    if not c.get("nullable", True):
        parts.append("NOT NULL")
    return " ".join(parts)


def render_structure(model: dict, schema: str) -> str:
    out = [f"CREATE SCHEMA IF NOT EXISTS {_ident(schema)};", f"SET search_path TO {_ident(schema)};", ""]
    tables = _ordered(model)
    for t in tables:
        lines = [_column_line(c) for c in t["columns"]]
        lines += [f"CONSTRAINT {_ident(k['name'])} {k['definition']}"
                  for k in t["constraints"] if k["kind"] in ("p", "u", "c")]
        out.append(f"CREATE TABLE {_qualified(schema, t['name'])} (\n  " + ",\n  ".join(lines) + "\n);")
        out.append("")
    for t in tables:
        qt = _qualified(schema, t["name"])
        if t.get("comment") is not None:
            out.append(f"COMMENT ON TABLE {qt} IS {_lit(t['comment'])};")
        for c in t["columns"]:
            if c.get("comment") is not None:
                out.append(f"COMMENT ON COLUMN {qt}.{_ident(c['name'])} IS {_lit(c['comment'])};")
    return "\n".join(out) + "\n"


def render_foreign_keys(model: dict, schema: str) -> str:
    out = [f"ALTER TABLE {_qualified(schema, t['name'])} ADD CONSTRAINT {_ident(k['name'])} {k['definition']};"
           for t in _ordered(model) for k in t["constraints"] if k["kind"] == "f"]
    return "\n".join(out) + "\n" if out else ""


def render_indexes(model: dict) -> str:
    out = [f"{i['definition']};" for t in _ordered(model) for i in t["indexes"]]
    return "\n".join(out) + "\n" if out else ""


def render_identity_fix(model: dict, schema: str) -> str:
    out = []
    for t in _ordered(model):
        qt = _qualified(schema, t["name"])
        for c in t["columns"]:
            if not c.get("identity"):
                continue
            col = _ident(c["name"])
            seq = (qt + "").replace("'", "''")
            name = c["name"].replace("'", "''")
            mx = f"(SELECT max({col}) FROM {qt})"
            out.append(f"SELECT setval(pg_get_serial_sequence('{seq}', '{name}'), "
                       f"COALESCE({mx}, 1), {mx} IS NOT NULL);")
    return "\n".join(out) + "\n" if out else ""


EXPORT_TIMEOUT_MS = 600_000


class _Chunks:
    """Iterator over the file's bytes; closing it (exhausted, error or early) closes the connection."""

    def __init__(self, export: "Export"):
        self._export = export
        self._gen = export._generate()

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        try:
            return next(self._gen)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._gen.close()
        self._export.close()


class Export:
    def __init__(self, schema: str, data: bool, conn, model: dict, counts: dict[str, int]):
        self.schema = schema
        self.data = data
        self.conn = conn
        self.model = model
        self.counts = counts

    def filename(self, today: datetime.date) -> str:
        return f"singularity-{self.schema}-{today.strftime('%Y%m%d')}.sql"

    def chunks(self) -> _Chunks:
        return _Chunks(self)

    def close(self) -> None:
        if not self.conn.closed:
            self.conn.close()

    def _generate(self):
        model, schema = self.model, self.schema
        yield render_header(schema, self.data, self.counts, datetime.date.today()).encode()
        yield ("\n" + render_structure(model, schema)).encode()
        if self.data:
            by_name = {t["name"]: t for t in model["tables"]}
            for name in table_order(model):
                cols = ", ".join(_ident(c["name"]) for c in by_name[name]["columns"])
                qt = _qualified(schema, name)
                yield f"\nCOPY {qt} ({cols}) FROM stdin;\n".encode()
                with self.conn.cursor() as cur:
                    with cur.copy(f"COPY {qt} ({cols}) TO STDOUT") as copy:
                        for chunk in copy:
                            yield bytes(chunk)
                yield b"\\.\n"
        fk = render_foreign_keys(model, schema)
        if fk:
            yield ("\n" + fk).encode()
        idx = render_indexes(model)
        if idx:
            yield ("\n" + idx).encode()
        if self.data:
            fix = render_identity_fix(model, schema)
            if fix:
                yield ("\n" + fix).encode()


def open_export(schema: str, *, data: bool, cap: int) -> Export:
    if schema not in allowed_schemas():
        raise ExportError(f"Unknown schema {schema!r}.", 400)
    conn = ro_connection(autocommit=True)
    try:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = {int(EXPORT_TIMEOUT_MS)}")
        model = read_model(conn, schema)
        counts: dict[str, int] = {}
        if data:
            total = 0
            for name in table_order(model):
                n = conn.execute(f"SELECT count(*) AS n FROM {_qualified(schema, name)}").fetchone()["n"]
                counts[name] = n
                total += n
                if total > cap:
                    raise ExportError(
                        f"Export is over the {cap}-row limit; the limit was crossed at table "
                        f"{_ident(name)}. Export without data or raise the limit.", 413)
        return Export(schema, data, conn, model, counts)
    except BaseException:
        conn.close()
        raise
