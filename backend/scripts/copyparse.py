"""Reader for PostgreSQL's COPY text format (what pg_dump writes after `COPY t (...) FROM stdin;`).

Rules (PostgreSQL docs, "COPY ... File Formats / Text Format"):
* one row per line, fields separated by a tab; a line that is exactly `\\.` ends the data;
* a field that is exactly `\\N` is NULL (`\\\\N` is the text `\\N`);
* backslash escapes: `\\b \\f \\n \\r \\t \\v`, `\\\\`; `\\` + 1-3 octal digits and `\\x` + 1-2 hex digits
  give a *byte* (several byte escapes may form one UTF-8 character); any other `\\X` is `X`.
Byte escapes that do not form valid UTF-8, or a NUL byte, are refused (PostgreSQL refuses them too).

Also home of ScriptIssue / RowStreamError, which are shared by scripts.sqlparse and scripts.mapping.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Iterator


@dataclass
class ScriptIssue:
    line: int
    statement: str
    message: str


class RowStreamError(Exception):
    """Raised *while iterating* a RowStream when a data row is bad (wrong number of fields, bad bytes).

    COPY rows are read lazily, so this cannot be reported by parse_script; consumers (mapping,
    validation, bulk load) must catch it and report `.issue` (it carries the data line number).
    """

    def __init__(self, issue: ScriptIssue):
        super().__init__(f"line {issue.line}: {issue.message}")
        self.issue = issue


def excerpt(s: str, limit: int = 200) -> str:
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 3] + "..."


_ESC = re.compile(r"\\(x[0-9A-Fa-f]{1,2}|[0-7]{1,3}|.)", re.S)
_CHARS = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


def unescape_field(f: str) -> str:
    """Decode one COPY text field (not `\\N`). Raises ValueError for bad bytes."""
    if "\\" not in f:
        return f
    out = bytearray()
    pos = 0
    for m in _ESC.finditer(f):
        out += f[pos:m.start()].encode("utf-8")
        s = m.group(1)
        if s[0] == "x" and len(s) > 1:
            out.append(int(s[1:], 16))
        elif s[0] in "01234567":
            out.append(int(s, 8) & 0xFF)
        else:
            out += _CHARS.get(s, s).encode("utf-8")
        pos = m.end()
    out += f[pos:].encode("utf-8")
    if 0 in out:
        raise ValueError("a NUL byte is not allowed in text")
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"escaped bytes are not valid UTF-8 ({exc.reason})") from None


def iter_copy_rows(lines: Iterable[str], *, width: int | None = None,
                   first_line: int = 1) -> Iterator[list[str | None]]:
    """Rows of a COPY text-format block given as lines (without "\\n"); stops at `\\.`.

    With `width`, a row with a different number of fields raises RowStreamError. Line numbers in
    errors count from `first_line` (the script line of the first data line).
    """
    for i, line in enumerate(lines):
        if line.endswith("\r"):
            line = line[:-1]
        if line == "\\.":
            return
        fields = line.split("\t")
        if width is not None and len(fields) != width:
            raise RowStreamError(ScriptIssue(first_line + i, excerpt(line),
                                             f"row has {len(fields)} fields, expected {width}"))
        try:
            row = [None if f == "\\N" else unescape_field(f) for f in fields]
        except ValueError as exc:
            raise RowStreamError(ScriptIssue(first_line + i, excerpt(line), str(exc))) from None
        yield row


def iter_lines(text: str, start: int = 0, end: int | None = None) -> Iterator[str]:
    """Lines of text[start:end] without their "\\n", lazily (a final "\\n" does not start a new line)."""
    end = len(text) if end is None else end
    while start < end:
        j = text.find("\n", start, end)
        if j == -1:
            j = end
        yield text[start:j]
        start = j + 1


def iter_copy(block: str) -> Iterator[list[str | None]]:
    """Rows of a COPY text-format data block (the text after `FROM stdin;`); stops at `\\.`."""
    return iter_copy_rows(iter_lines(block))
