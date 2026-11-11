"""CSV chunks for staged loads: RFC 4180 reading, COPY text output and per-cell checks.

Unquoted empty field = NULL; quoted `""` = empty string (the stdlib csv module cannot tell them apart).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

MAX_PROBLEMS = 20


class CsvError(Exception):
    def __init__(self, message: str, row: int):
        super().__init__(f"row {row}: {message}")
        self.message = message
        self.row = row


def parse_csv(text: str) -> list[list[str | None]]:
    rows: list[list[str | None]] = []
    row: list[str | None] = []
    buf: list[str] = []
    quoted = False      # the current field was opened with a quote
    in_q = False        # inside the quotes right now
    closed = False      # the closing quote was seen
    started = False     # the current field has any character
    n, i = len(text), 0

    def end_field():
        nonlocal buf, quoted, closed, started
        row.append("".join(buf) if (quoted or buf) else None)
        buf, quoted, closed, started = [], False, False, False

    while i < n:
        c = text[i]
        if in_q:
            if c == '"':
                if i + 1 < n and text[i + 1] == '"':
                    buf.append('"')
                    i += 1
                else:
                    in_q, closed = False, True
            else:
                buf.append(c)
        elif c == '"':
            if started or closed:
                raise CsvError("a quote may only open or close a quoted field", len(rows) + 1)
            quoted = in_q = started = True
        elif c == ",":
            end_field()
        elif c == "\n" or (c == "\r" and text[i + 1:i + 2] == "\n"):
            if c == "\r":
                i += 1
            if not row and not started and not quoted:
                raise CsvError("blank line", len(rows) + 1)
            end_field()
            rows.append(row)
            row = []
        elif closed:
            raise CsvError("text after a closing quote", len(rows) + 1)
        else:
            buf.append(c)
            started = True
        i += 1
    if in_q:
        raise CsvError("unterminated quote", len(rows) + 1)
    if row or started or quoted or buf:
        end_field()
        rows.append(row)
    return rows


def to_copy_line(row: list[str | None]) -> str:
    out = []
    for v in row:
        if v is None:
            out.append("\\N")
        else:
            out.append(v.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r"))
    return "\t".join(out)


_INT = re.compile(r"[+-]?\d+\Z")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_BOOL = {"t", "f", "true", "false", "yes", "no", "on", "off", "1", "0"}
_TS = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,6})?)?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?\Z")
_VARCHAR = re.compile(r"(?:varchar|character varying)\((\d+)\)\Z")
_TEXTY = ("text", "varchar", "character varying", "char", "character", "bpchar", "citext", "name")
_INTS = {"smallint": 32768, "integer": 2147483648, "bigint": 9223372036854775808}


def _bad_value(value: str, type_: str, enums: dict) -> str | None:
    t = type_.strip().lower()
    if t in _INTS:
        if not _INT.match(value.strip()):
            return f"not a whole number (type {t})"
        if not -_INTS[t] <= int(value) < _INTS[t]:
            return f"out of range for {t}"
        return None
    if t in ("numeric", "real", "double precision") or t.startswith("numeric("):
        try:
            d = Decimal(value.strip())
        except InvalidOperation:
            return f"not a number (type {t})"
        if not d.is_finite() and not t.startswith(("real", "double")):
            return f"NaN and infinity are not allowed for {t}"
        return None
    if t == "boolean":
        return None if value.strip().lower() in _BOOL else "not a boolean (use true, false, t, f, yes, no, 1 or 0)"
    if t == "uuid":
        return None if _UUID.match(value.strip()) else "not a uuid (8-4-4-4-12 hex digits)"
    if t in enums:
        return None if value in enums[t] else f"not one of {', '.join(enums[t])}"
    m = _VARCHAR.match(t)
    if m:
        return None if len(value) <= int(m.group(1)) else f"longer than {m.group(1)} characters"
    if t == "date" or t.startswith("timestamp"):
        m = _TS.match(value.strip())
        want = "YYYY-MM-DD" if t == "date" else "YYYY-MM-DD[ T]HH:MM[:SS[.ffffff]][Z|+HH[:MM]]"
        if not m or (t == "date" and m.group(2) is not None):
            return f"not a {t} (use {want})"
        try:
            date.fromisoformat(m.group(1))
            if m.group(2) is not None:
                datetime(2000, 1, 1, int(m.group(2)), int(m.group(3)), int(m.group(4) or 0))
        except ValueError:
            return f"not a real {t} (use {want})"
        return None
    return None


def _is_texty(type_: str) -> bool:
    t = type_.strip().lower()
    return t.startswith(_TEXTY)


def check_cells(rows, columns, enums, first_row):
    problems: list[dict] = []
    width = len(columns)
    for i, row in enumerate(rows):
        r = first_row + i
        if len(row) != width:
            problems.append({"row": r, "column": None, "value": None,
                             "message": f"expected {width} fields but found {len(row)}"})
        else:
            for c, v in zip(columns, row):
                if v == "" and not _is_texty(c["type"]):
                    v = None
                if v is None:
                    if c.get("required_value"):
                        problems.append({"row": r, "column": c["name"], "value": None,
                                         "message": "NULL is not allowed (NOT NULL column)"})
                    continue
                msg = _bad_value(v, c["type"], enums)
                if msg:
                    problems.append({"row": r, "column": c["name"], "value": v, "message": msg})
        if len(problems) >= MAX_PROBLEMS:
            return problems[:MAX_PROBLEMS]
    return problems
