"""Translate an uploaded SQL script (pg_dump style) into raw gate operations and row streams.

This module never executes SQL. It splits the text into statements by hand (pg_dump's
`COPY ... FROM stdin;` is followed by data lines that are not SQL, and psql meta lines such
as `\\restrict` are not SQL either), parses each statement with pglast, and maps the
allow-listed ones onto gate operations (spec section 2.3):

* CREATE TABLE                        -> create_table (PK / NOT NULL / DEFAULT / UNIQUE folded into columns)
* ALTER TABLE ... ADD CONSTRAINT      -> PRIMARY KEY folded into the column, add_unique, add_foreign_key, add_check
* ALTER TABLE ... ADD GENERATED AS IDENTITY -> identity flag on the column
* CREATE [UNIQUE] INDEX               -> add_index
* COMMENT ON TABLE / COLUMN           -> set_table_comment / set_column_comment
* COPY ... FROM stdin, INSERT ... VALUES -> a lazy RowStream per table (no op here)

Harmless boilerplate (SET, set_config/setval selects, OWNER TO, CREATE SCHEMA of the
script's own schema, BEGIN/COMMIT, psql meta lines) is listed in `ignored`. Everything
else is an error with its line number.

The ops are a *raw* translation: names are the script's own (schema stripped, so they land
in `master`). Renaming and the load mapping happen later (scripts.mapping).
"""
from __future__ import annotations

import bisect
import math
import re
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from itertools import chain, count, repeat
from typing import Callable, Iterator

import pglast
from pglast import ast, enums
from pglast.stream import RawStream

from scripts.copyparse import RowStreamError, ScriptIssue, iter_copy_rows  # noqa: F401 (re-exported)
from scripts.copyparse import excerpt as _excerpt, iter_lines as _iter_lines
from gate.operations import DEFAULT_FUNCS, IDENT_RE, TYPE_RE, check_expression, normalize_type


@dataclass
class RowStream:
    """Rows of one table from the script. `rows()` returns a fresh iterator each call.

    `lines()` (when set) yields the script line of each row, in the same order as `rows()`: a COPY
    data row is one line; every row of an INSERT ... VALUES has the INSERT's line.
    """
    table: str
    columns: list[str]
    rows: Callable[[], Iterator[list[str | None]]]
    lines: Callable[[], Iterator[int]] | None = None

    def numbered_rows(self) -> Iterator[tuple[int | None, list[str | None]]]:
        """(script line or None, row) pairs, lazily."""
        if self.lines is None:
            return ((None, r) for r in self.rows())
        return zip(self.lines(), self.rows())


@dataclass
class ParsedScript:
    ops: list[dict] = field(default_factory=list)
    streams: dict[str, RowStream] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)
    errors: list[ScriptIssue] = field(default_factory=list)
    statements: int = 0  # statements and psql meta lines seen (lone `;` not counted)


# ---------------------------------------------------------------- splitter

_TOK = re.compile(r"--|/\*|'|\"|\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?\$|;")
_COMMENT_TOK = re.compile(r"/\*|\*/")
_DQ = re.compile(r'"(?:[^"]|"")*"')
_SQ = re.compile(r"'(?:[^']|'')*'")
_ESQ = re.compile(r"'(?:[^'\\]|\\.|'')*'", re.S)
_WS = re.compile(r"\s+")
_COPY_TERM = re.compile(r"^\\\.$", re.M)
_COPY_HEAD = re.compile(r"copy\b", re.I)
_FROM_STDIN = re.compile(r"\bfrom\s+stdin\b", re.I)


def _identchar(c: str) -> bool:
    return c.isalnum() or c in "_$"


def _block_end(text: str, i: int) -> int | None:
    """End of the (nested) block comment starting at `i`, or None when it is not closed."""
    depth = 0
    for m in _COMMENT_TOK.finditer(text, i):
        depth += 1 if m.group() == "/*" else -1
        if depth == 0:
            return m.end()
    return None


def _end_block_comment(text: str, i: int) -> int:
    end = _block_end(text, i)
    return len(text) if end is None else end


def _skip_blank(text: str, pos: int) -> int:
    n = len(text)
    while pos < n:
        m = _WS.match(text, pos)
        if m:
            pos = m.end()
        elif text.startswith("--", pos):
            j = text.find("\n", pos)
            pos = n if j == -1 else j + 1
        elif text.startswith("/*", pos):
            pos = _end_block_comment(text, pos)
        else:
            break
    return pos


def _only_comments(rest: str) -> bool:
    """True when `rest` (one line) holds only whitespace and complete, nesting-aware comments."""
    pos, n = 0, len(rest)
    while pos < n:
        m = _WS.match(rest, pos)
        if m:
            pos = m.end()
        elif rest.startswith("--", pos):
            return True
        elif rest.startswith("/*", pos):
            end = _block_end(rest, pos)
            if end is None:
                return False  # unclosed /* on this line
            pos = end
        else:
            return False
    return True


def _statement_end(text: str, pos: int) -> int:
    """Index just after the `;` ending the statement that starts at `pos` (or len(text))."""
    n = len(text)
    while True:
        m = _TOK.search(text, pos)
        if not m:
            return n
        t, i = m.group(), m.start()
        if t == ";":
            return i + 1
        if t == "--":
            j = text.find("\n", i)
            pos = n if j == -1 else j + 1
        elif t == "/*":
            pos = _end_block_comment(text, i)
        elif t == '"':
            q = _DQ.match(text, i)
            pos = q.end() if q else n
        elif t == "'":
            escape = i > 0 and text[i - 1] in "Ee" and (i < 2 or not _identchar(text[i - 2]))
            q = (_ESQ if escape else _SQ).match(text, i)
            pos = q.end() if q else n
        else:  # $tag$
            if i > 0 and _identchar(text[i - 1]):
                pos = i + 1  # part of an identifier such as a$b, not a dollar quote
                continue
            j = text.find(t, m.end())
            pos = n if j == -1 else j + len(t)
        if pos >= n:
            return n


def _is_copy_from_stdin(stmt: str) -> bool:
    if not _COPY_HEAD.match(stmt):
        return False
    try:
        parsed = pglast.parse_sql(stmt)
    except pglast.parser.ParseError:
        return bool(_FROM_STDIN.search(stmt))
    if len(parsed) != 1 or not isinstance(parsed[0].stmt, ast.CopyStmt):
        return False
    c = parsed[0].stmt
    return bool(c.is_from) and c.filename is None and not c.is_program


@dataclass
class _Data:
    line: int
    start: int
    end: int
    terminated: bool


@dataclass
class _Piece:
    kind: str  # "meta" | "stmt"
    line: int
    text: str
    data: _Data | None = None
    trailing: str | None = None  # text after `COPY ... FROM stdin;` on the same line (not allowed)


def _split(text: str, line_of: Callable[[int], int]) -> Iterator[_Piece]:
    n = len(text)
    pos = 0
    while True:
        pos = _skip_blank(text, pos)
        if pos >= n:
            return
        if text[pos] == "\\":
            j = text.find("\n", pos)
            j = n if j == -1 else j
            yield _Piece("meta", line_of(pos), text[pos:j].rstrip())
            pos = j + 1
            continue
        end = _statement_end(text, pos)
        stmt = text[pos:end]
        piece = _Piece("stmt", line_of(pos), stmt)
        if _is_copy_from_stdin(stmt):
            j = text.find("\n", end)
            rest = text[end:n if j == -1 else j]
            if not _only_comments(rest):
                piece.trailing = rest.strip()
            ds = n if j == -1 else j + 1
            m = _COPY_TERM.search(text, ds)
            de = m.start() if m else n
            piece.data = _Data(line_of(ds) if ds < n else line_of(n), ds, de, m is not None)
            end = m.end() if m else n
        yield piece
        pos = end


# ---------------------------------------------------------------- translation helpers

class _Reject(Exception):
    pass


def _sql(node) -> str:
    try:
        return RawStream()(node)
    except Exception:  # noqa: BLE001 - only used for messages
        return type(node).__name__


_SIMPLE_TYPES = {
    "int4": "integer", "int8": "bigint", "int2": "smallint", "bool": "boolean", "text": "text", "date": "date",
    "timestamptz": "timestamptz", "timestamp": "timestamp", "uuid": "uuid", "jsonb": "jsonb",
    "float4": "real", "float8": "double precision",
}
_SERIAL_TYPES = {"serial": "integer", "serial4": "integer", "bigserial": "bigint", "serial8": "bigint",
                 "smallserial": "smallint", "serial2": "smallint"}
_ON_DELETE = {"a": "no action", "\x00": "no action", "r": "restrict", "c": "cascade", "n": "set null"}
_C = enums.ConstrType
_AT = enums.AlterTableType
_OBJ = enums.ObjectType


def _map_type(tn: ast.TypeName) -> tuple[str, bool]:
    """(gate type, is_serial) or _Reject naming the type."""
    shown = _sql(tn)
    names = [x.sval for x in tn.names if isinstance(x, ast.String)]
    if len(names) != len(tn.names) or not names or len(names) > 2 or (len(names) == 2 and names[0] != "pg_catalog"):
        raise _Reject(f"type {shown} is not supported")
    if tn.setof or tn.pct_type or tn.arrayBounds:
        raise _Reject(f"type {shown} is not supported (arrays are not allowed)")
    mods = []
    for m in tn.typmods or ():
        if not (isinstance(m, ast.A_Const) and isinstance(m.val, ast.Integer)):
            raise _Reject(f"type {shown} is not supported")
        mods.append(m.val.ival)
    base = names[-1]
    serial = base in _SERIAL_TYPES and len(names) == 1
    if serial and not mods:
        out = _SERIAL_TYPES[base]
    elif base in _SIMPLE_TYPES and not mods:
        out = _SIMPLE_TYPES[base]
    elif base == "varchar" and len(mods) <= 1:
        out = f"varchar({mods[0]})" if mods else "text"
    elif base == "numeric" and len(mods) <= 2:
        out = "numeric" if not mods else f"numeric({mods[0]},{mods[1] if len(mods) == 2 else 0})"
    else:
        raise _Reject(f"type {shown} is not supported (allowed: text, varchar(n), integer, bigint, smallint, "
                      "boolean, date, timestamp, timestamptz, uuid, jsonb, real, double precision, numeric(p,s), "
                      "serial, bigserial)")
    if not TYPE_RE.fullmatch(normalize_type(out)):
        raise _Reject(f"type {shown} is not supported")
    return out, serial


class _Numeric(str):
    """The exact source text of a non-integer numeric literal (never converted through float)."""


def _const(node) -> tuple[bool, object]:
    """(ok, python value) for a constant, optionally wrapped in a cast. Non-integer numbers -> _Numeric."""
    if isinstance(node, ast.TypeCast):
        node = node.arg
    if not isinstance(node, ast.A_Const):
        return False, None
    if node.isnull:
        return True, None
    v = node.val
    if isinstance(v, ast.Integer):
        return True, v.ival
    if isinstance(v, ast.Float):
        return True, int(v.fval) if re.fullmatch(r"-?\d+", v.fval) else _Numeric(v.fval)
    if isinstance(v, ast.Boolean):
        return True, bool(v.boolval)
    if isinstance(v, ast.String):
        return True, v.sval
    return False, None


_SVF = enums.SQLValueFunctionOp


def _map_default(expr):
    ok, v = _const(expr)
    if ok and isinstance(v, _Numeric):
        # a JSON float only when it is exact; otherwise keep the literal text (PostgreSQL casts it exactly)
        try:
            exact = Decimal(str(v))
        except InvalidOperation:
            raise _Reject(f"default {v} is not a valid number") from None
        if not exact.is_finite():
            raise _Reject(f"default {v} is not a finite number")
        f = float(exact)
        return f if math.isfinite(f) and Decimal(repr(f)) == exact else str(v)
    if ok:
        if isinstance(v, str) and v in DEFAULT_FUNCS:
            raise _Reject(f"default text {v!r} is not supported (it reads like a function call)")
        return v
    if isinstance(expr, ast.FuncCall) and not expr.args and not expr.agg_star:
        names = [x.sval for x in expr.funcname if isinstance(x, ast.String)]
        if len(names) in (1, 2) and (len(names) == 1 or names[0] == "pg_catalog") and \
                f"{names[-1]}()" in {"now()", "gen_random_uuid()"}:
            return f"{names[-1]}()"
    if isinstance(expr, ast.SQLValueFunction):
        if expr.op == _SVF.SVFOP_CURRENT_TIMESTAMP:
            return "now()"
        if expr.op == _SVF.SVFOP_CURRENT_DATE:
            return "current_date"
    raise _Reject(f"default {_sql(expr)} is not supported (allowed: constants, now(), CURRENT_TIMESTAMP, "
                  "CURRENT_DATE, gen_random_uuid())")


def _check_plain_constraint(c: ast.Constraint) -> None:
    if c.deferrable or c.initdeferred:
        raise _Reject("DEFERRABLE constraints are not supported")
    if c.skip_validation:
        raise _Reject("NOT VALID constraints are not supported")
    if c.is_no_inherit:
        raise _Reject("NO INHERIT constraints are not supported")
    if c.contype in (_C.CONSTR_CHECK, _C.CONSTR_FOREIGN) and c.is_enforced is False:
        raise _Reject("NOT ENFORCED constraints are not supported")
    if c.contype in (_C.CONSTR_PRIMARY, _C.CONSTR_UNIQUE) and (
            c.including or c.options or c.indexname or c.nulls_not_distinct or c.without_overlaps):
        raise _Reject("index options on PRIMARY KEY / UNIQUE constraints are not supported")


def _keys(c: ast.Constraint) -> list[str]:
    return [_ident(k.sval, "column") for k in c.keys or ()]


def _ident(name, what: str) -> str:
    """Source identifiers must already satisfy the gate's identifier rule (no silent renaming here)."""
    if not isinstance(name, str) or not IDENT_RE.fullmatch(name):
        raise _Reject(f"{what} {name!r} is not a valid identifier (letters, digits, underscore or hyphen, "
                      "starting with a letter or underscore, at most 63 characters)")
    return name


def _opt_ident(name):
    return None if name is None else _ident(name, "constraint name")


# Identifiers that could exceed 63 bytes: bare or double-quoted words. Quoted ones are
# only a candidate when they actually contain "" or are long, so this stays cheap on huge
# statements (``scan`` on a 300 KB VALUES list was quadratic: ~2 min, a 504 for the user).
# Double-quoted (or bare) identifiers only. Single-quoted text is a string literal, not an
# identifier, so it must never be reported -- Thai data at 3 bytes/char trips the 63-byte
# limit constantly. scan() made this distinction for free by its token filter; we can't,
# so skip anything inside single quotes by consuming them first.
_CANDIDATE_IDENT = re.compile(r"""'(?:[^']|'')*'|"(?:[^"]|"")*"?|\$[^$]*\$[^$]*\$|[A-Za-z_\u0080-\uffff][A-Za-z_0-9$\u0080-\uffff]*""")


def _long_identifier(stmt: str) -> str | None:
    """First identifier longer than 63 bytes in the source text (pglast would truncate it silently).

    Scanned with a regex rather than ``pglast.parser.scan``: the latter builds a token for
    every lexeme and copies the source per token, which is O(n^2) on a large VALUES list.
    """
    if len(stmt.encode("utf-8")) <= 63:  # nothing in here can be too long
        return None
    for word in _CANDIDATE_IDENT.findall(stmt):
        if word.startswith("'") or word.startswith("$"):
            continue  # string literal / dollar-quoted body, not an identifier
        name = word[1:-1].replace('""', '"') if word.startswith('"') else word
        if len(name.encode("utf-8")) > 63:
            return name
    return None


# ---------------------------------------------------------------- translator

class _Translator:
    def __init__(self, text: str):
        self.text = text
        self.schema: str | None = None
        self.pending_schema: str | None = None  # schema first seen in the current statement (kept if accepted)
        self.new_checks: list[dict] = []  # add_check ops built by the current statement
        self.tables: dict[str, dict] = {}  # name -> create_table op (mutated by later folds)
        self.uniques: list[dict] = []
        self.checks: list[dict] = []
        self.fks: list[dict] = []
        self.indexes: list[dict] = []
        self.comments: list[dict] = []
        self.stream_cols: dict[str, list[str]] = {}
        self.stream_src: dict[str, list[Callable[[], Iterator[list[str | None]]]]] = {}
        self.stream_lines: dict[str, list[Callable[[], Iterator[int]]]] = {}
        self.out = ParsedScript()

    # --- names and schema
    def _schema(self, name: str) -> None:
        current = self.schema or self.pending_schema
        if current is None:
            self.pending_schema = name
        elif name != current:
            raise _Reject(f"schema {name!r} is not allowed: this script already uses schema {current!r} "
                          "(only one schema per script; it is loaded into master)")

    def _schema_of_text(self, qualified: str) -> None:
        """Schema check for a name written inside a string, e.g. setval('sch.seq', ...)."""
        parts = [x.strip().strip('"') for x in qualified.split(".")]
        if len(parts) >= 2:
            self._schema(parts[-2])

    def _rel(self, rv: ast.RangeVar) -> str:
        if rv.catalogname:
            raise _Reject("database-qualified names are not allowed")
        if rv.relpersistence != "p":
            raise _Reject("temporary and unlogged tables are not allowed")
        if rv.schemaname:
            self._schema(rv.schemaname)
        return _ident(rv.relname, "table name")

    def _qualified(self, names: list[str], parts: int) -> list[str]:
        """Strip the schema from a dotted name of `parts` parts (+1 optional schema)."""
        if len(names) == parts + 1:
            self._schema(names[0])
            names = names[1:]
        elif len(names) != parts:
            raise _Reject("name has too many parts")
        return [_ident(n, "name") for n in names]

    def _script_table(self, name: str, what: str) -> dict:
        t = self.tables.get(name)
        if t is None:
            raise _Reject(f"{what} is only supported for tables created earlier in this script ({name!r} is not)")
        return t

    @staticmethod
    def _column(t: dict, name: str) -> dict:
        c = next((c for c in t["columns"] if c["name"] == name), None)
        if c is None:
            raise _Reject(f"table {t['name']!r} has no column {name!r}")
        return c

    # --- constraint pieces
    def _fk(self, table: str, c: ast.Constraint, column: str | None = None) -> dict:
        _check_plain_constraint(c)
        cols = [k.sval for k in c.fk_attrs] if c.fk_attrs else ([column] if column else [])
        refs = [k.sval for k in c.pk_attrs or ()]
        if len(cols) != 1 or len(refs) > 1:
            raise _Reject("composite foreign keys are not supported")
        if c.fk_matchtype not in ("s", "\x00"):
            raise _Reject("MATCH FULL / PARTIAL foreign keys are not supported")
        if c.fk_upd_action not in ("a", "\x00"):
            raise _Reject("ON UPDATE actions are not supported")
        if c.fk_del_action not in _ON_DELETE or c.fk_del_set_cols:
            raise _Reject("ON DELETE SET DEFAULT and column lists are not supported "
                          "(allowed: RESTRICT, CASCADE, SET NULL, NO ACTION)")
        return {"op": "add_foreign_key", "table": table, "column": _ident(cols[0], "column"),
                "ref_table": self._rel(c.pktable), "ref_column": _ident(refs[0], "column") if refs else None,
                "name": _opt_ident(c.conname),
                "on_delete": _ON_DELETE[c.fk_del_action]}

    def _check(self, table: str, c: ast.Constraint, column: str | None = None) -> dict:
        _check_plain_constraint(c)
        name = _opt_ident(c.conname) or (f"{table}_{column}_check" if column else f"{table}_check")[:63]
        op = {"op": "add_check", "table": table, "name": name, "expression": _sql(c.raw_expr)}
        self.new_checks.append(op)
        return op

    def _validate_checks(self, columns: list[str]) -> None:
        """Early, line-numbered version of the gate's check_expression test (the gate checks again)."""
        problems = [f"check {op['name']}: {p}" for op in self.new_checks
                    for p in check_expression(op["expression"], set(columns))]
        if problems:
            raise _Reject("; ".join(problems))

    def _table_constraint(self, table: str, c: ast.Constraint, create: dict | None, actions: list) -> None:
        """Queue the effect of a table-level constraint (from CREATE TABLE or ALTER TABLE ADD CONSTRAINT)."""
        _check_plain_constraint(c)
        if c.contype == _C.CONSTR_PRIMARY:
            keys = _keys(c)
            if len(keys) != 1:
                raise _Reject("composite primary keys are not supported")
            t = create or self._script_table(table, "ADD PRIMARY KEY")
            col = self._column(t, keys[0])
            other = next((x["name"] for x in t["columns"] if x.get("primary_key") and x is not col), None)
            if other:
                raise _Reject(f"table {table!r} already has a primary key ({other})")
            actions.append(lambda: col.update(primary_key=True, nullable=False))
        elif c.contype == _C.CONSTR_UNIQUE:
            keys = _keys(c)
            if not keys:
                raise _Reject("UNIQUE needs a column list")
            if create is not None and len(keys) == 1 and not c.conname:
                col = self._column(create, keys[0])
                actions.append(lambda: col.update(unique=True))
            else:
                op = {"op": "add_unique", "table": table, "columns": keys, "name": _opt_ident(c.conname)}
                actions.append(lambda: self.uniques.append(op))
        elif c.contype == _C.CONSTR_FOREIGN:
            op = self._fk(table, c)
            actions.append(lambda: self.fks.append(op))
        elif c.contype == _C.CONSTR_CHECK:
            op = self._check(table, c)
            actions.append(lambda: self.checks.append(op))
        else:
            raise _Reject(f"{c.contype.name.removeprefix('CONSTR_')} constraints are not supported")

    # --- statements
    def create_table(self, s: ast.CreateStmt) -> None:
        name = self._rel(s.relation)
        if (s.inhRelations or s.partbound or s.partspec or s.ofTypename or s.options or s.tablespacename
                or s.accessMethod or (s.oncommit and s.oncommit != enums.OnCommitAction.ONCOMMIT_NOOP)):
            raise _Reject("CREATE TABLE options (inheritance, partitions, storage, tablespace) are not supported")
        if s.if_not_exists:
            raise _Reject("CREATE TABLE IF NOT EXISTS is not supported")
        if name in self.tables:
            raise _Reject(f"table {name!r} is created twice")
        op = {"op": "create_table", "name": name, "columns": []}
        actions: list = []
        later: list[ast.Constraint] = []
        problems: list[str] = []  # report every bad column at once (e.g. all unknown types)
        for el in s.tableElts or ():
            if isinstance(el, ast.Constraint):
                later.append(el)
            elif isinstance(el, ast.ColumnDef):
                try:
                    op["columns"].append(self._column_def(name, el, actions))
                except _Reject as exc:
                    problems.append(f"column {el.colname}: {exc}")
            else:
                problems.append(f"{type(el).__name__} in CREATE TABLE is not supported")
        if problems:
            raise _Reject("; ".join(problems))
        if not op["columns"]:
            raise _Reject("table has no columns")
        names = [c["name"] for c in op["columns"]]
        if len(names) != len(set(names)):
            raise _Reject("duplicate column names")
        for c in later:
            self._table_constraint(name, c, op, actions)
        if sum(1 for c in s.tableElts if isinstance(c, ast.ColumnDef) and any(
                k.contype == _C.CONSTR_PRIMARY for k in c.constraints or ())) + sum(
                1 for c in later if c.contype == _C.CONSTR_PRIMARY) > 1:
            raise _Reject("a table can have only one primary key")
        self._validate_checks(names)
        self.tables[name] = op
        for a in actions:
            a()

    def _column_def(self, table: str, cd: ast.ColumnDef, actions: list) -> dict:
        if cd.collClause or cd.raw_default or cd.cooked_default or cd.fdwoptions or cd.compression:
            raise _Reject("COLLATE / compression / options are not supported")
        typ, serial = _map_type(cd.typeName)
        col: dict = {"name": _ident(cd.colname, "column name"), "type": typ}
        if serial:
            col["identity"] = True
        for c in cd.constraints or ():
            k = c.contype
            if k == _C.CONSTR_NOTNULL:
                col["nullable"] = False
            elif k == _C.CONSTR_NULL:
                pass
            elif k == _C.CONSTR_DEFAULT:
                col["default"] = _map_default(c.raw_expr)
            elif k == _C.CONSTR_IDENTITY:
                col["identity"] = True
            elif k == _C.CONSTR_PRIMARY:
                _check_plain_constraint(c)
                col["primary_key"] = True
                col["nullable"] = False
            elif k == _C.CONSTR_UNIQUE:
                _check_plain_constraint(c)
                col["unique"] = True
            elif k == _C.CONSTR_FOREIGN:
                op = self._fk(table, c, cd.colname)
                actions.append(lambda op=op: self.fks.append(op))
            elif k == _C.CONSTR_CHECK:
                op = self._check(table, c, cd.colname)
                actions.append(lambda op=op: self.checks.append(op))
            else:
                raise _Reject(f"{k.name.removeprefix('CONSTR_')} is not supported")
        return col

    def alter_table(self, s: ast.AlterTableStmt) -> bool:
        """Returns True when the statement is only OWNER TO (ignored)."""
        owner = [c.subtype == _AT.AT_ChangeOwner for c in s.cmds]
        if all(owner):
            self._rel(s.relation)  # schema check only
            return True
        if any(owner):
            raise _Reject("OWNER TO cannot be combined with other actions in one ALTER statement "
                          "(write it as its own statement; OWNER TO is ignored)")
        if s.objtype != _OBJ.OBJECT_TABLE:
            raise _Reject(f"ALTER {s.objtype.name.removeprefix('OBJECT_')} is not allowed in a script")
        table = self._rel(s.relation)
        actions: list = []
        for cmd in s.cmds:
            if cmd.subtype == _AT.AT_AddConstraint:
                self._table_constraint(table, cmd.def_, None, actions)
            elif cmd.subtype == _AT.AT_AddIdentity:
                col = self._column(self._script_table(table, "ADD GENERATED AS IDENTITY"),
                                   _ident(cmd.name, "column"))
                actions.append(lambda col=col: col.update(identity=True))
            else:
                what = cmd.subtype.name.removeprefix("AT_")
                raise _Reject(f"ALTER TABLE {what} is not supported (allowed: ADD CONSTRAINT PRIMARY KEY / "
                              "UNIQUE / FOREIGN KEY / CHECK, ADD GENERATED AS IDENTITY, OWNER TO)")
        if self.new_checks and table in self.tables:
            self._validate_checks([c["name"] for c in self.tables[table]["columns"]])
        for a in actions:
            a()
        return False

    def create_index(self, s: ast.IndexStmt) -> None:
        table = self._rel(s.relation)
        if s.concurrent:
            raise _Reject("CREATE INDEX CONCURRENTLY is not supported (the load runs in one transaction)")
        if s.if_not_exists:
            raise _Reject("CREATE INDEX IF NOT EXISTS is not supported")
        if not s.relation.inh:
            raise _Reject("CREATE INDEX ON ONLY (partitioned tables) is not supported")
        if not s.idxname:
            raise _Reject("CREATE INDEX needs an index name")
        if s.accessMethod not in (None, "btree"):
            raise _Reject(f"index method {s.accessMethod} is not supported (only btree)")
        if s.whereClause or s.indexIncludingParams or s.options or s.tableSpace or s.nulls_not_distinct \
                or s.excludeOpNames:
            raise _Reject("partial indexes, INCLUDE, WITH and TABLESPACE are not supported")
        cols = []
        for e in s.indexParams:
            if (not e.name or e.expr or e.collation or e.opclass or e.opclassopts
                    or e.ordering != enums.SortByDir.SORTBY_DEFAULT
                    or e.nulls_ordering != enums.SortByNulls.SORTBY_NULLS_DEFAULT):
                raise _Reject("only plain column indexes are supported (no expressions, ordering or operator classes)")
            cols.append(_ident(e.name, "column"))
        op = {"op": "add_index", "table": table, "columns": cols, "name": _ident(s.idxname, "index name")}
        if s.unique:
            op["unique"] = True
        self.indexes.append(op)

    def comment(self, s: ast.CommentStmt) -> bool:
        """Returns True when ignored (comment on the script's schema)."""
        if s.objtype == _OBJ.OBJECT_SCHEMA and isinstance(s.object, ast.String):
            self._schema(s.object.sval)  # the script's own schema only; ignored
            return True
        if s.objtype not in (_OBJ.OBJECT_TABLE, _OBJ.OBJECT_COLUMN):
            raise _Reject(f"COMMENT ON {s.objtype.name.removeprefix('OBJECT_')} is not allowed "
                          "(only COMMENT ON TABLE / COLUMN)")
        if s.comment is None:
            raise _Reject("removing a comment (IS NULL) is not supported")
        names = [x.sval for x in s.object]
        if s.objtype == _OBJ.OBJECT_TABLE:
            [table] = self._qualified(names, 1)
            self.comments.append({"op": "set_table_comment", "table": table, "comment": s.comment})
        else:
            table, column = self._qualified(names, 2)
            self.comments.append({"op": "set_column_comment", "table": table, "column": column,
                                  "comment": s.comment})
        return False

    def _stream_columns(self, table: str, given: list[str] | None, what: str) -> list[str]:
        if given:
            return given
        t = self.tables.get(table)
        if t is None:
            raise _Reject(f"{what} without a column list needs CREATE TABLE {table} earlier in the script")
        return [c["name"] for c in t["columns"]]

    def _add_stream(self, table: str, columns: list[str], src, lines) -> None:
        for c in columns:
            _ident(c, "column")
        if len(columns) != len(set(columns)):
            raise _Reject("duplicate columns")
        have = self.stream_cols.get(table)
        if have is not None and have != columns:
            raise _Reject(f"rows for {table!r} were already given with columns {have}; "
                          "all data for a table must use the same column list")
        self.stream_cols[table] = columns
        self.stream_src.setdefault(table, []).append(src)
        self.stream_lines.setdefault(table, []).append(lines)

    def copy(self, s: ast.CopyStmt, data: _Data | None) -> None:
        if s.query or not s.relation:
            raise _Reject("COPY (query) is not allowed")
        if not s.is_from or s.filename is not None or s.is_program:
            raise _Reject("only COPY ... FROM stdin is allowed")
        if s.options or s.whereClause:
            raise _Reject("COPY options are not supported (text format only)")
        table = self._rel(s.relation)
        cols = self._stream_columns(table, [x.sval for x in s.attlist or ()], "COPY")
        if data is None:  # cannot happen: the splitter marks every COPY FROM stdin
            raise _Reject("COPY has no data block")
        text, start, end, first, width = self.text, data.start, data.end, data.line, len(cols)
        self._add_stream(table, cols, lambda: iter_copy_rows(_iter_lines(text, start, end), width=width,
                                                              first_line=first),
                         lambda: count(first))

    def insert(self, s: ast.InsertStmt, line: int) -> None:
        if s.withClause or s.onConflictClause or s.returningClause:
            raise _Reject("INSERT with WITH / ON CONFLICT / RETURNING is not supported")
        table = self._rel(s.relation)
        sel = s.selectStmt
        if not isinstance(sel, ast.SelectStmt) or not sel.valuesLists or any(getattr(sel, f) for f in (
                "targetList", "fromClause", "whereClause", "groupClause", "havingClause", "sortClause",
                "limitCount", "limitOffset", "withClause", "larg", "rarg", "distinctClause")):
            raise _Reject("only INSERT ... VALUES is supported")
        given = []
        for rt in s.cols or ():
            if rt.indirection:
                raise _Reject("INSERT into sub-fields is not supported")
            given.append(rt.name)
        cols = self._stream_columns(table, given, "INSERT")
        rows: list[list[str | None]] = []
        for vals in sel.valuesLists:
            if len(vals) != len(cols):
                raise _Reject(f"INSERT row has {len(vals)} values for {len(cols)} columns")
            row: list[str | None] = []
            for v in vals:
                ok, val = _const(v)
                if not ok:
                    raise _Reject(f"INSERT values must be constants (found {_sql(v)})")
                row.append(None if val is None else ("true" if val is True else "false" if val is False else str(val)))
            rows.append(row)
        self._add_stream(table, cols, lambda: iter(rows), lambda: repeat(line, len(rows)))

    def create_schema(self, s: ast.CreateSchemaStmt) -> None:
        if s.schemaElts:
            raise _Reject("CREATE SCHEMA with elements is not supported")
        if not s.schemaname:
            raise _Reject("CREATE SCHEMA needs a name")
        self._schema(s.schemaname)

    # --- dispatch
    def statement(self, piece: _Piece) -> bool:
        """Translate one statement. Returns False for an empty statement (a lone `;`)."""
        excerpt = _excerpt(piece.text)
        try:
            parsed = pglast.parse_sql(piece.text)
        except pglast.parser.ParseError as exc:
            self.out.errors.append(ScriptIssue(piece.line, excerpt, f"not valid SQL: {exc}"))
            return True
        if not parsed:
            return False
        if len(parsed) != 1:
            self.out.errors.append(ScriptIssue(piece.line, excerpt, "could not split this statement"))
            return True
        s = parsed[0].stmt
        self.pending_schema = None
        self.new_checks = []
        try:
            long = _long_identifier(piece.text)
            if long is not None:
                raise _Reject(f"identifier {long!r} is longer than 63 characters")
            ignored = self._dispatch(s, piece)
        except _Reject as exc:
            self.out.errors.append(ScriptIssue(piece.line, excerpt, str(exc)))
            return True
        if self.pending_schema is not None:
            self.schema = self.pending_schema  # only an accepted statement fixes the source schema
        if ignored:
            self.out.ignored.append(f"line {piece.line}: {excerpt}")
        return True

    def _dispatch(self, s, piece: _Piece) -> bool:
        if isinstance(s, ast.VariableSetStmt):
            self.set_stmt(s)
            return True
        if isinstance(s, ast.SelectStmt) and self.boilerplate_select(s):
            return True
        if isinstance(s, ast.TransactionStmt) and s.kind in (
                enums.TransactionStmtKind.TRANS_STMT_BEGIN, enums.TransactionStmtKind.TRANS_STMT_START,
                enums.TransactionStmtKind.TRANS_STMT_COMMIT):
            return True
        if isinstance(s, ast.AlterOwnerStmt):
            self.alter_owner(s)
            return True
        if isinstance(s, ast.CreateTableAsStmt):
            raise _Reject("CREATE TABLE AS / SELECT INTO is not allowed (create the table, then load rows "
                          "with COPY ... FROM stdin or INSERT ... VALUES)")
        if isinstance(s, ast.CreateSchemaStmt):
            self.create_schema(s)
            return True
        if isinstance(s, ast.CreateStmt):
            self.create_table(s)
        elif isinstance(s, ast.AlterTableStmt):
            return self.alter_table(s)
        elif isinstance(s, ast.IndexStmt):
            self.create_index(s)
        elif isinstance(s, ast.CommentStmt):
            return self.comment(s)
        elif isinstance(s, ast.CopyStmt):
            self.copy(s, piece.data)
        elif isinstance(s, ast.InsertStmt):
            self.insert(s, piece.line)
        elif isinstance(s, ast.SelectStmt):
            raise _Reject("SELECT is not allowed in a script (only pg_dump's set_config('search_path', ...) and "
                          "setval(...) with constant arguments are ignored)")
        else:
            words = " ".join(piece.text.split()[:2]).rstrip(";").upper()
            raise _Reject(f"{words} is not allowed in a script (allowed: CREATE TABLE, ALTER TABLE ... ADD "
                          "CONSTRAINT, CREATE INDEX, COMMENT ON TABLE/COLUMN, COPY ... FROM stdin, "
                          "INSERT ... VALUES)")
        return False

    # --- boilerplate that is ignored, with the checks that keep it harmless
    def set_stmt(self, s: ast.VariableSetStmt) -> None:
        name = (s.name or "").lower()
        K = enums.VariableSetKind
        vals = []
        for a in s.args or ():
            ok, v = _const(a)
            vals.append(str(v).lower() if ok and not isinstance(v, bool) else
                        ("on" if v is True else "off" if v is False else None))
        resetting = s.kind in (K.VAR_SET_DEFAULT, K.VAR_RESET)
        if name == "search_path":
            if resetting or s.kind != K.VAR_SET_VALUE or any(v is None for v in vals):
                raise _Reject("SET search_path is only allowed with '' or the script's own schema")
            for a in s.args:
                self._search_path_entry(str(_const(a)[1]))
        elif name == "standard_conforming_strings":
            if not resetting and vals not in (["on"], ["true"], ["yes"], ["1"]):
                raise _Reject("SET standard_conforming_strings = off is not supported "
                              "(it changes how string literals are read)")
        elif name == "backslash_quote":
            if not resetting and vals != ["safe_encoding"]:
                raise _Reject("SET backslash_quote is not supported (it changes how string literals are read)")
        elif name == "client_encoding":
            if not resetting and (len(vals) != 1 or (vals[0] or "").replace("-", "").replace("_", "")
                                  not in ("utf8", "unicode")):
                raise _Reject("the script must be UTF-8 (SET client_encoding to another encoding is not supported)")

    def _search_path_entry(self, entry: str) -> None:
        """One search_path entry: '' / pg_catalog / "$user" are fine, anything else must be the source schema."""
        entry = entry.strip()
        if entry.startswith('"') and entry.endswith('"') and len(entry) >= 2:
            entry = entry[1:-1].replace('""', '"')
        if entry in ("", "pg_catalog", "$user"):
            return
        self._schema(entry)

    def boilerplate_select(self, s: ast.SelectStmt) -> bool:
        """pg_dump's `SELECT pg_catalog.set_config('search_path', '', false)` and
        `SELECT pg_catalog.setval(<'seq' | pg_get_serial_sequence('t', 'c')>, n[, bool])`."""
        if any(getattr(s, f) for f in ("fromClause", "whereClause", "groupClause", "havingClause", "sortClause",
                                       "withClause", "larg", "rarg", "valuesLists", "intoClause", "limitCount",
                                       "limitOffset", "distinctClause", "lockingClause")):
            return False
        if not s.targetList or len(s.targetList) != 1:
            return False
        f = s.targetList[0].val
        fname = _catalog_func(f)
        if fname == "set_config":
            args = f.args or ()
            if len(args) != 3 or not all(_const(a)[0] for a in args):
                raise _Reject("set_config is only allowed as set_config('search_path', '<schema>', false)")
            key, value = _const(args[0])[1], _const(args[1])[1]
            if key != "search_path":
                raise _Reject(f"set_config({key!r}, ...) is not supported (only 'search_path')")
            if not isinstance(value, str):
                raise _Reject("set_config('search_path', ...) needs a text value ('' or the script's schema)")
            for part in value.split(","):
                self._search_path_entry(part)
            return True
        if fname == "setval":
            args = list(f.args or ())
            if not 2 <= len(args) <= 3 or not all(_const(a)[0] for a in args[1:]):
                return False
            first = args[0]
            ok, seq = _const(first)
            if not ok and _catalog_func(first) == "pg_get_serial_sequence" and len(first.args or ()) == 2 \
                    and all(_const(a)[0] for a in first.args):
                ok, seq = _const(first.args[0])
            if not ok or not isinstance(seq, str):
                return False
            self._schema_of_text(seq)
            return True
        return False

    def alter_owner(self, s: ast.AlterOwnerStmt) -> None:
        obj = s.object
        if s.objectType == _OBJ.OBJECT_SCHEMA and isinstance(obj, ast.String):
            self._schema(obj.sval)
            return
        if isinstance(obj, ast.ObjectWithArgs):
            names = [x.sval for x in obj.objname or () if isinstance(x, ast.String)]
        elif isinstance(obj, (list, tuple)):
            names = [x.sval for x in obj if isinstance(x, ast.String)]
        else:
            names = []
        if len(names) >= 2:
            self._schema(names[-2])

    # --- result
    def finish(self) -> ParsedScript:
        order = list(self.tables)
        deps = {t: {f["ref_table"] for f in self.fks if f["table"] == t and f["ref_table"] in self.tables
                    and f["ref_table"] != t} for t in order}
        done: list[str] = []
        pending = order[:]
        while pending:
            nxt = next((t for t in pending if deps[t] <= set(done)), pending[0])  # cycle: keep script order
            done.append(nxt)
            pending.remove(nxt)
        unique_idx = [o for o in self.indexes if o.get("unique")]
        plain_idx = [o for o in self.indexes if not o.get("unique")]
        # unique constraints and unique indexes before foreign keys, which may reference them
        self.out.ops = ([self.tables[t] for t in done] + self.uniques + unique_idx + self.checks + self.fks
                        + plain_idx + self.comments)
        for table, cols in self.stream_cols.items():
            srcs, lines = self.stream_src[table], self.stream_lines[table]
            self.out.streams[table] = RowStream(
                table, cols, (lambda srcs=srcs: chain.from_iterable(src() for src in srcs)),
                (lambda lines=lines: chain.from_iterable(ln() for ln in lines)))
        return self.out


def _catalog_func(f) -> str | None:
    """Name of a plain call `f(...)` / `pg_catalog.f(...)`, else None."""
    if not isinstance(f, ast.FuncCall) or f.agg_star or f.agg_distinct or f.over or f.agg_filter or f.agg_order:
        return None
    names = [x.sval for x in f.funcname if isinstance(x, ast.String)]
    if len(names) == 1 or (len(names) == 2 and names[0] == "pg_catalog"):
        return names[-1]
    return None


# psql meta commands that would pull in content we cannot see; everything else is ignored.
_META_ERRORS = {"i", "ir", "include", "include_relative", "copy", "!", "gexec"}
_META_CMD = re.compile(r"\\([A-Za-z_!]+)")


def parse_script(text: str) -> ParsedScript:
    """Split and translate `text` (see module docstring). Never raises for bad SQL; see `.errors`."""
    if text.startswith("﻿"):
        text = text[1:]
    text = text.replace("\r\n", "\n")
    tr = _Translator(text)
    if "\x00" in text:
        tr.out.errors.append(ScriptIssue(1, "", "script contains NUL bytes"))
        return tr.out
    starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line_of(offset: int) -> int:
        return bisect.bisect_right(starts, offset)

    seen = 0
    for piece in _split(text, line_of):
        seen += 1
        if piece.kind == "meta":
            m = _META_CMD.match(piece.text)
            if m and m.group(1) in _META_ERRORS:
                tr.out.errors.append(ScriptIssue(piece.line, _excerpt(piece.text),
                                                 f"psql command \\{m.group(1)} is not allowed in a script"))
            else:
                tr.out.ignored.append(f"line {piece.line}: {_excerpt(piece.text)}")
            continue
        if not tr.statement(piece):
            seen -= 1
        if piece.trailing:
            tr.out.errors.append(ScriptIssue(piece.line, _excerpt(piece.trailing),
                                             "text after `COPY ... FROM stdin;` on the same line is not allowed "
                                             "(the data starts on the next line)"))
        if piece.data is not None and not piece.data.terminated:
            tr.out.errors.append(ScriptIssue(piece.line, _excerpt(piece.text),
                                             "COPY data is not terminated by a line containing only \\."))
    if not seen:
        tr.out.errors.append(ScriptIssue(1, "", "no statements"))
    out = tr.finish()
    out.statements = seen
    return out
