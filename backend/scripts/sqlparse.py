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
import re
from dataclasses import dataclass, field
from itertools import chain
from typing import Callable, Iterable, Iterator

import pglast
from pglast import ast, enums
from pglast.stream import RawStream

from gate.operations import DEFAULT_FUNCS, TYPE_RE, normalize_type


@dataclass
class ScriptIssue:
    line: int
    statement: str
    message: str


@dataclass
class RowStream:
    """Rows of one table from the script. `rows()` returns a fresh iterator each call."""
    table: str
    columns: list[str]
    rows: Callable[[], Iterator[list[str | None]]]


@dataclass
class ParsedScript:
    ops: list[dict] = field(default_factory=list)
    streams: dict[str, RowStream] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)
    errors: list[ScriptIssue] = field(default_factory=list)


# ---------------------------------------------------------------- COPY text format

_COPY_ESC = re.compile(r"\\(x[0-9A-Fa-f]{1,2}|[0-7]{1,3}|.)", re.S)
_COPY_CHARS = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


def _copy_unescape(m: re.Match) -> str:
    s = m.group(1)
    if s[0] == "x" and len(s) > 1:
        return chr(int(s[1:], 16))
    if s[0] in "01234567":
        return chr(int(s, 8) & 0xFF)
    return _COPY_CHARS.get(s, s)


def iter_copy_rows(lines: Iterable[str]) -> Iterator[list[str | None]]:
    """Rows of a COPY text-format block: tab separated, `\\N` = NULL, backslash escapes; stops at `\\.`."""
    for line in lines:
        if line.endswith("\r"):
            line = line[:-1]
        if line == "\\.":
            return
        yield [None if f == "\\N" else (_COPY_ESC.sub(_copy_unescape, f) if "\\" in f else f)
               for f in line.split("\t")]


def _iter_lines(text: str, start: int, end: int) -> Iterator[str]:
    while start < end:
        j = text.find("\n", start, end)
        if j == -1:
            j = end
        yield text[start:j]
        start = j + 1


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


def _end_block_comment(text: str, i: int) -> int:
    depth = 0
    for m in _COMMENT_TOK.finditer(text, i):
        depth += 1 if m.group() == "/*" else -1
        if depth == 0:
            return m.end()
    return len(text)


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


def _excerpt(s: str, limit: int = 200) -> str:
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 3] + "..."


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
    if not TYPE_RE.match(normalize_type(out)):
        raise _Reject(f"type {shown} is not supported")
    return out, serial


def _const(node) -> tuple[bool, object]:
    """(ok, python value) for a constant, optionally wrapped in a cast."""
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
        return True, int(v.fval) if re.fullmatch(r"-?\d+", v.fval) else float(v.fval)
    if isinstance(v, ast.Boolean):
        return True, bool(v.boolval)
    if isinstance(v, ast.String):
        return True, v.sval
    return False, None


_SVF = enums.SQLValueFunctionOp


def _map_default(expr):
    ok, v = _const(expr)
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
    return [k.sval for k in c.keys or ()]


# ---------------------------------------------------------------- translator

class _Translator:
    def __init__(self, text: str):
        self.text = text
        self.schema: str | None = None
        self.tables: dict[str, dict] = {}  # name -> create_table op (mutated by later folds)
        self.uniques: list[dict] = []
        self.checks: list[dict] = []
        self.fks: list[dict] = []
        self.indexes: list[dict] = []
        self.comments: list[dict] = []
        self.stream_cols: dict[str, list[str]] = {}
        self.stream_src: dict[str, list[Callable[[], Iterator[list[str | None]]]]] = {}
        self.out = ParsedScript()

    # --- names and schema
    def _schema(self, name: str) -> None:
        if self.schema is None:
            self.schema = name
        elif name != self.schema:
            raise _Reject(f"schema {name!r} is not allowed: this script already uses schema {self.schema!r} "
                          "(only one schema per script; it is loaded into master)")

    def _rel(self, rv: ast.RangeVar) -> str:
        if rv.catalogname:
            raise _Reject("database-qualified names are not allowed")
        if rv.relpersistence != "p":
            raise _Reject("temporary and unlogged tables are not allowed")
        if rv.schemaname:
            self._schema(rv.schemaname)
        return rv.relname

    def _qualified(self, names: list[str], parts: int) -> list[str]:
        """Strip the schema from a dotted name of `parts` parts (+1 optional schema)."""
        if len(names) == parts + 1:
            self._schema(names[0])
            return names[1:]
        if len(names) != parts:
            raise _Reject("name has too many parts")
        return names

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
        return {"op": "add_foreign_key", "table": table, "column": cols[0], "ref_table": self._rel(c.pktable),
                "ref_column": refs[0] if refs else None, "name": c.conname,
                "on_delete": _ON_DELETE[c.fk_del_action]}

    @staticmethod
    def _check(table: str, c: ast.Constraint, column: str | None = None) -> dict:
        _check_plain_constraint(c)
        name = c.conname or (f"{table}_{column}_check" if column else f"{table}_check")[:63]
        return {"op": "add_check", "table": table, "name": name, "expression": _sql(c.raw_expr)}

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
                op = {"op": "add_unique", "table": table, "columns": keys, "name": c.conname}
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
        self.tables[name] = op
        for a in actions:
            a()

    def _column_def(self, table: str, cd: ast.ColumnDef, actions: list) -> dict:
        if cd.collClause or cd.raw_default or cd.cooked_default or cd.fdwoptions or cd.compression:
            raise _Reject("COLLATE / compression / options are not supported")
        typ, serial = _map_type(cd.typeName)
        col: dict = {"name": cd.colname, "type": typ}
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
        if all(c.subtype == _AT.AT_ChangeOwner for c in s.cmds):
            return True
        if s.objtype != _OBJ.OBJECT_TABLE:
            raise _Reject(f"ALTER {s.objtype.name.removeprefix('OBJECT_')} is not allowed in a script")
        table = self._rel(s.relation)
        actions: list = []
        for cmd in s.cmds:
            if cmd.subtype == _AT.AT_AddConstraint:
                self._table_constraint(table, cmd.def_, None, actions)
            elif cmd.subtype == _AT.AT_AddIdentity:
                col = self._column(self._script_table(table, "ADD GENERATED AS IDENTITY"), cmd.name)
                actions.append(lambda col=col: col.update(identity=True))
            else:
                what = cmd.subtype.name.removeprefix("AT_")
                raise _Reject(f"ALTER TABLE {what} is not supported (allowed: ADD CONSTRAINT PRIMARY KEY / "
                              "UNIQUE / FOREIGN KEY / CHECK, ADD GENERATED AS IDENTITY, OWNER TO)")
        for a in actions:
            a()
        return False

    def create_index(self, s: ast.IndexStmt) -> None:
        table = self._rel(s.relation)
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
            cols.append(e.name)
        op = {"op": "add_index", "table": table, "columns": cols, "name": s.idxname}
        if s.unique:
            op["unique"] = True
        self.indexes.append(op)

    def comment(self, s: ast.CommentStmt) -> bool:
        """Returns True when ignored (comment on the script's schema)."""
        if s.objtype == _OBJ.OBJECT_SCHEMA and isinstance(s.object, ast.String) and \
                (self.schema is None or s.object.sval == self.schema):
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

    def _add_stream(self, table: str, columns: list[str], src) -> None:
        if len(columns) != len(set(columns)):
            raise _Reject("duplicate columns")
        have = self.stream_cols.get(table)
        if have is not None and have != columns:
            raise _Reject(f"rows for {table!r} were already given with columns {have}; "
                          "all data for a table must use the same column list")
        self.stream_cols[table] = columns
        self.stream_src.setdefault(table, []).append(src)

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
        text, start, end = self.text, data.start, data.end
        self._add_stream(table, cols, lambda: iter_copy_rows(_iter_lines(text, start, end)))

    def insert(self, s: ast.InsertStmt) -> None:
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
        self._add_stream(table, cols, lambda: iter(rows))

    def create_schema(self, s: ast.CreateSchemaStmt) -> None:
        if s.schemaElts:
            raise _Reject("CREATE SCHEMA with elements is not supported")
        if not s.schemaname:
            raise _Reject("CREATE SCHEMA needs a name")
        self._schema(s.schemaname)

    # --- dispatch
    def statement(self, piece: _Piece) -> None:
        excerpt = _excerpt(piece.text)
        try:
            parsed = pglast.parse_sql(piece.text)
        except pglast.parser.ParseError as exc:
            self.out.errors.append(ScriptIssue(piece.line, excerpt, f"not valid SQL: {exc}"))
            return
        if not parsed:
            return  # empty statement (lone ';')
        if len(parsed) != 1:
            self.out.errors.append(ScriptIssue(piece.line, excerpt, "could not split this statement"))
            return
        s = parsed[0].stmt
        try:
            ignored = self._dispatch(s, piece)
        except _Reject as exc:
            self.out.errors.append(ScriptIssue(piece.line, excerpt, str(exc)))
            return
        if ignored:
            self.out.ignored.append(f"line {piece.line}: {excerpt}")

    def _dispatch(self, s, piece: _Piece) -> bool:
        if isinstance(s, ast.VariableSetStmt):
            return True
        if isinstance(s, ast.SelectStmt) and _is_boilerplate_select(s):
            return True
        if isinstance(s, ast.TransactionStmt) and s.kind in (
                enums.TransactionStmtKind.TRANS_STMT_BEGIN, enums.TransactionStmtKind.TRANS_STMT_START,
                enums.TransactionStmtKind.TRANS_STMT_COMMIT):
            return True
        if isinstance(s, ast.AlterOwnerStmt):
            return True
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
            self.insert(s)
        else:
            words = " ".join(piece.text.split()[:2]).rstrip(";").upper()
            raise _Reject(f"{words} is not allowed in a script (allowed: CREATE TABLE, ALTER TABLE ... ADD "
                          "CONSTRAINT, CREATE INDEX, COMMENT ON TABLE/COLUMN, COPY ... FROM stdin, "
                          "INSERT ... VALUES)")
        return False

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
        self.out.ops = ([self.tables[t] for t in done] + self.uniques + self.checks + self.fks + self.indexes
                        + self.comments)
        for table, cols in self.stream_cols.items():
            srcs = self.stream_src[table]
            self.out.streams[table] = RowStream(
                table, cols, (lambda srcs=srcs: chain.from_iterable(src() for src in srcs)))
        return self.out


def _is_boilerplate_select(s: ast.SelectStmt) -> bool:
    """`SELECT pg_catalog.set_config(...)` / `SELECT pg_catalog.setval(...)` as written by pg_dump."""
    if any(getattr(s, f) for f in ("fromClause", "whereClause", "groupClause", "havingClause", "sortClause",
                                   "withClause", "larg", "rarg", "valuesLists", "intoClause")):
        return False
    if not s.targetList or len(s.targetList) != 1:
        return False
    f = s.targetList[0].val
    if not isinstance(f, ast.FuncCall):
        return False
    names = [x.sval for x in f.funcname if isinstance(x, ast.String)]
    return names in (["set_config"], ["pg_catalog", "set_config"], ["setval"], ["pg_catalog", "setval"]) and all(
        _const(a)[0] for a in f.args or ())


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
        tr.statement(piece)
        if piece.data is not None and not piece.data.terminated:
            tr.out.errors.append(ScriptIssue(piece.line, _excerpt(piece.text),
                                             "COPY data is not terminated by a line containing only \\."))
    if not seen:
        tr.out.errors.append(ScriptIssue(1, "", "no statements"))
    return tr.finish()
