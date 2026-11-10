"""Structured operations: the only way anything changes `master` (spec section 2).

The AI never sends write SQL. It sends a list of operations (plain dicts); this
module checks their *shape*. Semantic checks (does the table exist?) live in
`gate.state`, naming/type rules in `gate.rules`, and SQL in `gate.sqlgen`.
"""
from __future__ import annotations

import re

# Any name that is safe to quote: the naming *style* is a rule (change it through the MCP), not a regex here.
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,62}\Z")

# Only these column types may be created. Anything else is rejected, so a type
# string can never smuggle SQL into a statement.
TYPE_RE = re.compile(
    r"^(text|integer|bigint|smallint|boolean|date|timestamptz|timestamp|uuid|jsonb|real|"
    r"double precision|numeric|numeric\(\d{1,2},\s?\d{1,2}\)|varchar\(\d{1,4}\))\Z"
)

# Defaults: JSON literals or one of these function calls. Nothing free-form.
DEFAULT_FUNCS = {"now()", "gen_random_uuid()", "current_date"}

ON_DELETE_VALUES = ("restrict", "cascade", "set null", "no action")

# add_check expressions: the only functions and operators they may use.
CHECK_FUNCS = {"char_length", "length", "lower", "upper", "coalesce", "abs"}
CHECK_OPS = {"=", "<>", "!=", "<", ">", "<=", ">=", "~", "~*", "!~", "!~*", "+", "-", "*", "/", "%", "||"}
CHECK_COMPARISONS = {"=", "<>", "!=", "<", ">", "<=", ">=", "~", "~*", "!~", "!~*"}
CHECK_CAST_TYPES = {"text", "varchar", "int2", "int4", "int8", "numeric", "float4", "float8", "bool", "date",
                    "timestamp", "timestamptz", "uuid"}
MAX_CHECK_LEN = 1000

SCHEMA_OPS = {
    "create_table", "add_column", "rename_table", "rename_column", "alter_column_type",
    "set_column_comment", "add_index", "add_foreign_key", "add_unique",
    "drop_column", "drop_table", "drop_index", "drop_constraint",
    "add_check", "set_table_comment",
    "set_column_default", "drop_column_default", "set_not_null", "drop_not_null",
    "create_view", "drop_view",
}
DATA_OPS = {"insert_rows", "update_rows", "deactivate_rows", "bulk_insert"}
# Produced only by script intake; the normal propose path refuses it (see gate.check.run allow_bulk).
INTERNAL_OPS = {"bulk_insert"}
# Database-level ops: one per change, never mixed with table or data ops.
DATABASE_OPS = {"create_schema", "rename_schema", "drop_schema"}
ALL_OPS = SCHEMA_OPS | DATA_OPS | DATABASE_OPS
DESTRUCTIVE_OPS = {"drop_column", "drop_table", "drop_index", "drop_constraint", "drop_schema", "drop_view"}

SCHEMA_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}\Z")
RESERVED_SCHEMAS = frozenset({"singularity", "public", "information_schema"})


def valid_schema_name(name) -> str | None:
    """Return an error message when `name` cannot be a managed schema name, else None."""
    if not isinstance(name, str) or not SCHEMA_NAME_RE.match(name):
        return (f"schema name {name!r} must match ^[a-z][a-z0-9_]{{0,62}}$ "
                "(lowercase letters, digits and underscores, starting with a letter)")
    if name in RESERVED_SCHEMAS or name.startswith("pg_"):
        return f"schema name {name!r} is reserved (singularity, public, information_schema and pg_* are refused)"
    return None

SCALAR = (str, int, float, bool, type(None))


class OpError(Exception):
    """One or more operations are malformed. `.errors` lists every problem found."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def normalize_type(t: str) -> str:
    return re.sub(r"\s+", " ", t.strip().lower()).replace(", ", ",")


MAX_VIEW_LEN = 10000


def view_references(query: str) -> set[str]:
    """Unqualified relation names a view query reads (CTE names excluded). The query must already be valid."""
    import pglast
    from pglast import ast
    from pglast.visitors import Visitor

    found: set[str] = set()
    ctes: set[str] = set()

    class V(Visitor):
        def visit_RangeVar(self, ancestors, node):
            if node.schemaname is None:
                found.add(node.relname)

        def visit_CommonTableExpr(self, ancestors, node):
            ctes.add(node.ctename)

    V()(pglast.parse_sql(query))
    return found - ctes


def check_view_query(query) -> list[str]:
    """Problems with a view query (empty list = safe to render).

    It must parse as exactly one SELECT (set operations and plain CTEs are fine) with no INTO, no
    locking clause and no data-modifying statement anywhere inside it.
    """
    import pglast
    from pglast import ast
    from pglast.visitors import Visitor

    if not isinstance(query, str) or not query.strip():
        return ["view query must be non-empty text"]
    if len(query) > MAX_VIEW_LEN:
        return [f"view query is longer than {MAX_VIEW_LEN} characters"]
    try:
        parsed = pglast.parse_sql(query)
    except pglast.parser.ParseError as exc:
        return [f"view query does not parse: {exc}"]
    if len(parsed) != 1 or not isinstance(parsed[0].stmt, ast.SelectStmt):
        return ["view query must be exactly one SELECT statement"]
    problems: list[str] = []

    class V(Visitor):
        def visit_SelectStmt(self, ancestors, node):
            if node.intoClause is not None:
                problems.append("view query must not use SELECT ... INTO")

        def visit_LockingClause(self, ancestors, node):
            problems.append("view query must not use FOR UPDATE / FOR SHARE")

        def _write(self, ancestors, node):
            problems.append("view query must not contain INSERT, UPDATE, DELETE or MERGE")

        visit_InsertStmt = visit_UpdateStmt = visit_DeleteStmt = visit_MergeStmt = _write

    V()(parsed)
    return sorted(set(problems))


def _ident(v, what: str, errs: list[str], where: str) -> None:
    if not isinstance(v, str) or not IDENT_RE.fullmatch(v):
        errs.append(f"{where}: {what} must be an identifier (letters, digits, underscore or hyphen; max 63 chars)")


def _check_default(d, errs: list[str], where: str) -> None:
    if isinstance(d, str) and d in DEFAULT_FUNCS:
        return
    if not isinstance(d, SCALAR):
        errs.append(f"{where}: default must be a JSON scalar or one of {sorted(DEFAULT_FUNCS)}")


def _check_column(col, errs: list[str], where: str, *, allow_references: bool = True) -> None:
    if not isinstance(col, dict):
        errs.append(f"{where}: column must be an object")
        return
    unknown = set(col) - {
        "name", "type", "nullable", "unique", "default", "comment", "references", "primary_key", "identity",
    }
    if unknown:
        errs.append(f"{where}: unknown column keys {sorted(unknown)}")
    _ident(col.get("name"), "column name", errs, where)
    t = col.get("type")
    if not isinstance(t, str) or not TYPE_RE.fullmatch(normalize_type(t)):
        errs.append(f"{where}: unsupported type {t!r}")
    for flag in ("nullable", "unique", "primary_key", "identity"):
        if flag in col and not isinstance(col[flag], bool):
            errs.append(f"{where}: {flag} must be true or false")
    if "default" in col:
        _check_default(col["default"], errs, where)
    if "comment" in col and not isinstance(col["comment"], str):
        errs.append(f"{where}: comment must be text")
    ref = col.get("references")
    if ref is not None:
        if not allow_references or not isinstance(ref, dict) or set(ref) - {"table", "column", "on_delete"}:
            errs.append(f"{where}: references must be {{table, column?, on_delete?}}")
        else:
            _ident(ref.get("table"), "references.table", errs, where)
            _on_delete(ref, errs, where)
            if ref.get("column") is not None:
                _ident(ref["column"], "references.column", errs, where)


def _on_delete(d: dict, errs: list[str], where: str) -> None:
    if "on_delete" in d and d["on_delete"] not in ON_DELETE_VALUES:
        errs.append(f"{where}: on_delete must be one of {list(ON_DELETE_VALUES)}")


# The parser wraps `LIKE .. ESCAPE` / `SIMILAR TO` patterns in these pg_catalog functions (keyed by A_Expr_Kind name).
_ESCAPE_FUNCS = {"AEXPR_LIKE": "like_escape", "AEXPR_ILIKE": "like_escape", "AEXPR_SIMILAR": "similar_to_escape"}


def check_expression(expr: str, columns: set[str]) -> list[str]:
    """Problems with a CHECK expression (empty list = safe to render).

    The text must parse (pglast) as exactly one boolean expression that only uses
    plain column references of the table, constants, allow-listed operators and
    functions, and no subqueries. Anything not explicitly handled is rejected.
    """
    import pglast
    from pglast import ast, enums

    if not isinstance(expr, str) or not expr.strip():
        return ["expression must be non-empty text"]
    if len(expr) > MAX_CHECK_LEN:
        return [f"expression is longer than {MAX_CHECK_LEN} characters"]
    # Intentionally conservative: comment markers are refused anywhere, even inside string literals.
    if "--" in expr or "/*" in expr:
        return ["expression must not contain comments"]
    try:
        stmts = pglast.parse_sql(f"SELECT ({expr})")
    except pglast.parser.ParseError as exc:
        return [f"expression is not valid SQL: {exc}"]
    sel = stmts[0].stmt if len(stmts) == 1 else None
    plain = (
        isinstance(sel, ast.SelectStmt) and sel.targetList and len(sel.targetList) == 1
        and not any(getattr(sel, f) for f in (
            "fromClause", "whereClause", "groupClause", "havingClause", "windowClause", "sortClause",
            "limitCount", "limitOffset", "lockingClause", "withClause", "intoClause", "distinctClause",
            "valuesLists", "larg", "rarg"))
    )
    if not plain:
        return ["expression must be a single expression"]
    if sel.targetList[0].name is not None:
        return ["expression must be a single expression (no alias)"]
    top = sel.targetList[0].val

    errs: list[str] = []
    K = enums.A_Expr_Kind

    def bad(msg):
        if msg not in errs:
            errs.append(msg)

    def op_name(e) -> str | None:
        names = [n.sval for n in e.name] if e.name else []
        return names[0] if len(names) == 1 and isinstance(e.name[0], ast.String) else None

    def plain_call(f) -> bool:
        """A function call with no FILTER / OVER / DISTINCT / ORDER BY / VARIADIC / * decorations."""
        return not (f.agg_order or f.agg_filter or f.over or f.agg_within_group or f.agg_star
                    or f.agg_distinct or f.func_variadic)

    def walk(n):
        if isinstance(n, ast.ColumnRef):
            f = n.fields
            if len(f) != 1 or not isinstance(f[0], ast.String):
                bad("column references must be a plain column name of the table")
            elif f[0].sval not in columns:
                bad(f"unknown column {f[0].sval!r}")
        elif isinstance(n, ast.A_Const):
            pass
        elif isinstance(n, ast.BoolExpr):
            for a in n.args:
                walk(a)
        elif isinstance(n, ast.NullTest | ast.BooleanTest):
            walk(n.arg)
        elif isinstance(n, ast.CoalesceExpr):
            for a in n.args:
                walk(a)
        elif isinstance(n, ast.TypeCast):
            t = [x.sval for x in n.typeName.names if isinstance(x, ast.String)]
            qualified_ok = len(t) == 1 or (len(t) == 2 and t[0] == "pg_catalog")
            if (n.typeName.setof or n.typeName.arrayBounds or not qualified_ok
                    or t[-1] not in CHECK_CAST_TYPES):
                bad("type cast to this type is not allowed")
            for m in n.typeName.typmods or ():
                if not (isinstance(m, ast.A_Const) and isinstance(m.val, ast.Integer)):
                    bad("type modifiers must be plain integers")
            walk(n.arg)
        elif isinstance(n, ast.FuncCall):
            names = [x.sval for x in n.funcname if isinstance(x, ast.String)]
            fname = names[-1] if names else None
            if (len(n.funcname) > 2 or (len(names) == 2 and names[0] != "pg_catalog")
                    or fname not in CHECK_FUNCS):
                bad(f"function {'.'.join(names)}() is not allowed (allowed: {sorted(CHECK_FUNCS)})")
            if not plain_call(n):
                bad("aggregate, window and special function forms are not allowed")
            for a in n.args or ():
                walk(a)
        elif isinstance(n, ast.A_Expr):
            name = op_name(n)
            if n.kind == K.AEXPR_OP:
                if name not in CHECK_OPS:
                    bad(f"operator {name!r} is not allowed")
            elif n.kind in (K.AEXPR_IN, K.AEXPR_LIKE, K.AEXPR_ILIKE, K.AEXPR_SIMILAR, K.AEXPR_BETWEEN,
                            K.AEXPR_NOT_BETWEEN, K.AEXPR_BETWEEN_SYM, K.AEXPR_NOT_BETWEEN_SYM,
                            K.AEXPR_DISTINCT, K.AEXPR_NOT_DISTINCT):
                pass
            else:
                bad("this operator form is not allowed")
            walk(n.lexpr)
            esc = _ESCAPE_FUNCS.get(n.kind.name)
            rx = n.rexpr
            if (esc and isinstance(rx, ast.FuncCall) and plain_call(rx)
                    and [x.sval for x in rx.funcname if isinstance(x, ast.String)] == ["pg_catalog", esc]
                    and len(rx.funcname) == 2):
                # LIKE ... ESCAPE / SIMILAR TO: the parser wraps the pattern in this function; only that spot is exempt
                for a in rx.args or ():
                    walk(a)
            elif isinstance(n.rexpr, tuple | list):
                for a in n.rexpr:
                    walk(a)
            else:
                walk(n.rexpr)
        elif n is None:
            pass
        else:
            bad(f"{type(n).__name__} is not allowed in a check expression (no subqueries, parameters or other constructs)")

    walk(top)
    if errs:
        return errs

    # must be boolean: a comparison/logic/null-test node, a boolean constant, or a cast to bool
    # (a bare column is refused because types are unknown here; write `flag IS TRUE`)
    ok = isinstance(top, ast.BoolExpr | ast.NullTest | ast.BooleanTest)
    if isinstance(top, ast.A_Expr):
        ok = top.kind != K.AEXPR_OP or op_name(top) in CHECK_COMPARISONS
    elif isinstance(top, ast.A_Const):
        ok = isinstance(top.val, ast.Boolean)
    elif isinstance(top, ast.TypeCast):
        ok = top.typeName.names[-1].sval == "bool"
    if not ok:
        return ["expression must be a boolean condition (for example a comparison)"]
    return []


def _check_where(w, errs: list[str], where: str) -> None:
    if not isinstance(w, dict) or not w:
        errs.append(f"{where}: where must be a non-empty object (updates without a filter are not allowed)")
        return
    for k, v in w.items():
        _ident(k, "where column", errs, where)
        if isinstance(v, list):
            if not v or not all(isinstance(x, SCALAR) and x is not None for x in v):
                errs.append(f"{where}: where.{k} list must contain non-null scalars")
        elif not isinstance(v, SCALAR) or v is None:
            errs.append(f"{where}: where.{k} must be a non-null scalar or a list of them")


def _check_values(vals, errs: list[str], where: str, what: str) -> None:
    if not isinstance(vals, dict) or not vals:
        errs.append(f"{where}: {what} must be a non-empty object")
        return
    for k, v in vals.items():
        _ident(k, f"{what} column", errs, where)
        if not isinstance(v, (*SCALAR, dict, list)):
            errs.append(f"{where}: {what}.{k} must be a JSON value")


def _check_cols_list(cols, errs: list[str], where: str) -> None:
    if not isinstance(cols, list) or not cols:
        errs.append(f"{where}: columns must be a non-empty list")
        return
    for c in cols:
        _ident(c, "column", errs, where)


def _reason(op, errs, where):
    r = op.get("reason")
    if not isinstance(r, str) or len(r.strip()) < 3:
        errs.append(f"{where}: destructive operations need a 'reason'")


def parse_ops(raw, *, max_ops: int = 50) -> list[dict]:
    """Validate shape of an operation list. Returns the list unchanged or raises OpError."""
    errs: list[str] = []
    if not isinstance(raw, list) or not raw:
        raise OpError(["operations must be a non-empty list"])
    if len(raw) > max_ops:
        raise OpError([f"too many operations ({len(raw)} > {max_ops})"])

    for i, op in enumerate(raw):
        where = f"op[{i}]"
        if not isinstance(op, dict):
            errs.append(f"{where}: must be an object")
            continue
        kind = op.get("op")
        if kind not in ALL_OPS:
            errs.append(f"{where}: unknown op {kind!r}")
            continue
        where = f"op[{i}] {kind}"
        allowed = {"op"}

        def keys(*names):
            allowed.update(names)

        if kind == "create_table":
            keys("name", "columns", "comment")
            _ident(op.get("name"), "table name", errs, where)
            cols = op.get("columns")
            if not isinstance(cols, list) or not cols:
                errs.append(f"{where}: columns must be a non-empty list")
            else:
                for c in cols:
                    _check_column(c, errs, where)
                names = [c.get("name") for c in cols if isinstance(c, dict)]
                if len(names) != len(set(names)):
                    errs.append(f"{where}: duplicate column names")
            if "comment" in op and not isinstance(op["comment"], str):
                errs.append(f"{where}: comment must be text")
        elif kind == "create_schema":
            keys("name", "comment")
            msg = valid_schema_name(op.get("name"))
            if msg:
                errs.append(f"{where}: {msg}")
            if "comment" in op and not isinstance(op["comment"], str):
                errs.append(f"{where}: comment must be text")
        elif kind == "rename_schema":
            keys("schema", "new_name")
            for key in ("schema", "new_name"):
                msg = valid_schema_name(op.get(key))
                if msg:
                    errs.append(f"{where}: {key}: {msg}")
        elif kind == "drop_schema":
            keys("schema", "reason")
            msg = valid_schema_name(op.get("schema"))
            if msg:
                errs.append(f"{where}: schema: {msg}")
            _reason(op, errs, where)
        elif kind == "add_column":
            keys("table", "column")
            _ident(op.get("table"), "table", errs, where)
            _check_column(op.get("column"), errs, where)
        elif kind == "rename_table":
            keys("table", "new_name")
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("new_name"), "new_name", errs, where)
        elif kind == "rename_column":
            keys("table", "column", "new_name")
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("column"), "column", errs, where)
            _ident(op.get("new_name"), "new_name", errs, where)
        elif kind == "alter_column_type":
            keys("table", "column", "type")
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("column"), "column", errs, where)
            if not isinstance(op.get("type"), str) or not TYPE_RE.fullmatch(normalize_type(op["type"])):
                errs.append(f"{where}: unsupported type {op.get('type')!r}")
        elif kind in ("set_column_default", "drop_column_default", "set_not_null", "drop_not_null"):
            keys("table", "column", *(["default"] if kind == "set_column_default" else []))
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("column"), "column", errs, where)
            if kind == "set_column_default":
                if "default" not in op:
                    errs.append(f"{where}: default is required")
                else:
                    _check_default(op["default"], errs, where)
        elif kind == "create_view":
            keys("name", "query", "comment")
            _ident(op.get("name"), "view name", errs, where)
            errs.extend(f"{where}: {p}" for p in check_view_query(op.get("query")))
            if "comment" in op and not isinstance(op["comment"], str):
                errs.append(f"{where}: comment must be text")
        elif kind == "drop_view":
            keys("name", "reason")
            _ident(op.get("name"), "view name", errs, where)
            _reason(op, errs, where)
        elif kind == "set_column_comment":
            keys("table", "column", "comment")
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("column"), "column", errs, where)
            if not isinstance(op.get("comment"), str):
                errs.append(f"{where}: comment must be text")
        elif kind in ("add_index", "add_unique"):
            keys("table", "columns", "name", *(["unique"] if kind == "add_index" else []))
            _ident(op.get("table"), "table", errs, where)
            _check_cols_list(op.get("columns"), errs, where)
            if op.get("name") is not None:
                _ident(op["name"], "name", errs, where)
            if "unique" in op and not isinstance(op["unique"], bool):
                errs.append(f"{where}: unique must be true or false")
        elif kind == "add_foreign_key":
            keys("table", "column", "ref_table", "ref_column", "name", "on_delete")
            _on_delete(op, errs, where)
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("column"), "column", errs, where)
            _ident(op.get("ref_table"), "ref_table", errs, where)
            if op.get("ref_column") is not None:
                _ident(op["ref_column"], "ref_column", errs, where)
            if op.get("name") is not None:
                _ident(op["name"], "name", errs, where)
        elif kind == "add_check":
            keys("table", "name", "expression")
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("name"), "name", errs, where)
            if not isinstance(op.get("expression"), str) or not op["expression"].strip():
                errs.append(f"{where}: expression must be non-empty text")
        elif kind == "set_table_comment":
            keys("table", "comment")
            _ident(op.get("table"), "table", errs, where)
            if not isinstance(op.get("comment"), str):
                errs.append(f"{where}: comment must be text")
        elif kind == "drop_column":
            keys("table", "column", "reason")
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("column"), "column", errs, where)
            _reason(op, errs, where)
        elif kind == "drop_table":
            keys("table", "reason")
            _ident(op.get("table"), "table", errs, where)
            _reason(op, errs, where)
        elif kind == "drop_index":
            keys("name", "reason")
            _ident(op.get("name"), "name", errs, where)
            _reason(op, errs, where)
        elif kind == "drop_constraint":
            keys("table", "name", "reason")
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("name"), "name", errs, where)
            _reason(op, errs, where)
        elif kind == "insert_rows":
            keys("table", "rows")
            _ident(op.get("table"), "table", errs, where)
            rows = op.get("rows")
            if not isinstance(rows, list) or not rows:
                errs.append(f"{where}: rows must be a non-empty list")
            else:
                for r in rows:
                    _check_values(r, errs, where, "row")
        elif kind == "bulk_insert":
            keys("table", "columns", "source", "refs", "expected_rows")
            _ident(op.get("table"), "table", errs, where)
            _check_cols_list(op.get("columns"), errs, where)
            cols = op.get("columns")
            if isinstance(cols, list) and len(cols) != len(set(cols)):
                errs.append(f"{where}: duplicate columns")
            if isinstance(cols, list) and "__row" in cols:
                errs.append(f"{where}: column name '__row' is reserved")
            # source.columns: the stream's columns, one per op column, in op column order (the row
            # provider projects stream rows with it). script_id is null only between mapping
            # (scripts.mapping) and binding to a stored script (scripts.mapping.bind_script);
            # a bulk_insert with a null script_id has no rows to read.
            src = op.get("source")
            if (not isinstance(src, dict) or set(src) != {"script_id", "stream", "columns"}
                    or not (src["script_id"] is None
                            or (isinstance(src["script_id"], int) and not isinstance(src["script_id"], bool)))
                    or not isinstance(src["stream"], str) or not src["stream"]
                    or not isinstance(src["columns"], list)):
                errs.append(f"{where}: source must be {{script_id: int|null, stream: text, columns: [stream column]}}")
            else:
                for c in src["columns"]:
                    _ident(c, "source column", errs, where)
                if isinstance(cols, list) and len(src["columns"]) != len(cols):
                    errs.append(f"{where}: source.columns must have one stream column per column")
            er = op.get("expected_rows")
            if not isinstance(er, int) or isinstance(er, bool) or er < 1:
                errs.append(f"{where}: expected_rows must be a positive integer")
            refs = op.get("refs", [])
            if not isinstance(refs, list):
                errs.append(f"{where}: refs must be a list")
            else:
                seen = set()
                for r in refs:
                    if not isinstance(r, dict) or set(r) != {"column", "parent_table", "parent_key", "source_column"}:
                        errs.append(f"{where}: each ref must be {{column, parent_table, parent_key, source_column}}")
                        continue
                    for f in ("column", "parent_table", "parent_key", "source_column"):
                        _ident(r[f], f"ref {f}", errs, where)
                    if isinstance(cols, list) and r["source_column"] not in cols:
                        errs.append(f"{where}: ref source_column {r['source_column']!r} is not in columns")
                    if r["column"] in seen:
                        errs.append(f"{where}: column {r['column']!r} has more than one ref")
                    seen.add(r["column"])
        elif kind == "update_rows":
            keys("table", "where", "set")
            _ident(op.get("table"), "table", errs, where)
            _check_where(op.get("where"), errs, where)
            _check_values(op.get("set"), errs, where, "set")
        elif kind == "deactivate_rows":
            keys("table", "where")
            _ident(op.get("table"), "table", errs, where)
            _check_where(op.get("where"), errs, where)

        extra = set(op) - allowed
        if extra:
            errs.append(f"{where}: unknown keys {sorted(extra)}")

    kinds = {op.get("op") for op in raw if isinstance(op, dict)}
    if kinds & DATABASE_OPS and (len(raw) > 1):
        errs.append("schema operations (create_schema, rename_schema, drop_schema) must be proposed alone, "
                    "one per change, not mixed with other operations")

    if errs:
        raise OpError(errs)
    return raw


def kind_of(ops: list[dict]) -> str:
    """'schema' if any operation changes structure, else 'data'."""
    return "schema" if any(o["op"] in SCHEMA_OPS or o["op"] in DATABASE_OPS for o in ops) else "data"


def affected_tables(ops: list[dict]) -> list[str]:
    out: set[str] = set()
    for o in ops:
        if o["op"] in DATABASE_OPS:
            continue
        if o["op"] == "create_table":
            out.add(o["name"])
        elif o["op"] == "rename_table":
            out.update((o["table"], o["new_name"]))
        elif o["op"] == "drop_index":
            continue  # index name only; table not known from the op
        else:
            out.add(o["table"])
        if o["op"] == "add_foreign_key":
            out.add(o["ref_table"])
        if o["op"] == "bulk_insert":
            out.update(r["parent_table"] for r in o.get("refs", []))
        if o["op"] == "create_table":
            for c in o["columns"]:
                if c.get("references"):
                    out.add(c["references"]["table"])
        if o["op"] == "add_column" and o["column"].get("references"):
            out.add(o["column"]["references"]["table"])
    return sorted(out)
