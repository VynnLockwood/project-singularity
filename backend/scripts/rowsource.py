"""Row provider for bulk_insert ops: reads a stored script's rows back, lazily, in op column order.

Used by the validation dry run (scripts.validate) and by the real apply of a script change.

    row_source = make_row_source(script)
    executor.execute(ops, state, actor, row_source=row_source, ...)

`row_source(op)` checks the op against the script (bound script id, stream exists, every
`op["source"]["columns"]` is a stream column) when it is called, then returns a generator of tuples,
one value per op column (`op["columns"][i]` <- stream column `op["source"]["columns"][i]`).
The script file is read (checksum-verified by scripts.storage.read) and parsed once, on the first call;
rows are streamed from the text, never collected into a list.

Every problem is raised as schema.executor.ApplyError, so the executor rolls back and reports it
like any other failure. A bad COPY row names its script line.
"""
from __future__ import annotations

from typing import Callable, Iterator

from schema.executor import ApplyError

from . import storage
from .copyparse import RowStreamError
from .models import Script
from .sqlparse import ParsedScript, parse_script


def make_row_source(script: Script) -> Callable[[dict], Iterator[tuple]]:
    parsed: list[ParsedScript] = []

    def load() -> ParsedScript:
        if not parsed:
            try:
                data = storage.read(script)
            except storage.ScriptError as exc:
                raise ApplyError(f"script #{script.pk}: {exc}") from None
            p = parse_script(data.decode("utf-8"))
            if p.errors:
                e = p.errors[0]
                raise ApplyError(f"script #{script.pk} no longer parses: line {e.line}: {e.message}")
            parsed.append(p)
        return parsed[0]

    def row_source(op: dict) -> Iterator[tuple]:
        src = op.get("source") or {}
        if src.get("script_id") != script.pk:
            raise ApplyError(f"bulk_insert into {op.get('table')}: rows belong to script "
                             f"#{src.get('script_id')}, not script #{script.pk}")
        stream = load().streams.get(src.get("stream"))
        if stream is None:
            raise ApplyError(f"bulk_insert into {op.get('table')}: the script has no rows for {src.get('stream')!r}")
        idx = []
        for c in src.get("columns", []):
            if c not in stream.columns:
                raise ApplyError(f"bulk_insert into {op.get('table')}: script rows for {stream.table!r} have no "
                                 f"column {c!r} (columns: {', '.join(stream.columns)})")
            idx.append(stream.columns.index(c))
        return _rows(stream, idx, op.get("table"))

    return row_source


def _rows(stream, idx: list[int], table) -> Iterator[tuple]:
    try:
        for row in stream.rows():
            yield tuple(row[i] for i in idx)
    except RowStreamError as exc:
        i = exc.issue
        raise ApplyError(f"script line {i.line}: bad row for {stream.table!r} (loading {table}): {i.message}") from None
