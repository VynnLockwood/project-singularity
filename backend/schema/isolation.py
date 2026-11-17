"""SQL isolation: `query`, intelligent query, raw SQL and scripts cannot reach databases the user lacks.

Every statement runs with `search_path` set to one database, but a qualified name (`crm.t`) still resolves, so
names are checked in the app (no per-database Postgres roles, by decision). Two layers:

- the text (pglast): `named_schemas` lists every database a statement names (relations, types, functions,
  operators, collations, the targets of COMMENT/ALTER/GRANT/DROP/CREATE, names inside string literals given
  to name-taking functions such as `nextval('crm.s')` or casts such as `'crm.t'::regclass`, and the SQL
  inside sql/plpgsql bodies). A shape whose names cannot be determined (dynamic SQL, functions that run SQL
  text or turn object ids into names, a statement kind not listed here, SQL that does not parse) raises
  `Unclear`: callers refuse it for non-admins (fail closed). `catalog_names_leak` says whether a statement
  reads the system catalogs (`pg_*`, `information_schema`), which list every database's names.
- the plan: `check_plan` runs `EXPLAIN (VERBOSE, FORMAT JSON)` and refuses any relation outside the allowed
  databases anywhere in the plan tree (subplans, CTEs, InitPlans, function and foreign scans). Views are
  expanded by the planner, so a view in a readable database over another database is refused too.

Hidden equals missing: a named database the user cannot reach is refused with the very text of one that does
not exist (`introspect.unknown_schema_message`); the check never asks whether it exists. The other refusal
texts name nothing outside the user's own SQL.

Known, accepted gap: a function in a readable database that itself reads another database is not detected
(the plan shows only the call). See README "Database access".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import pglast
from pglast import ast, enums
from pglast.stream import RawStream

from . import introspect


class QueryError(Exception):
    """A refused or failed read-only query (the text is safe to show)."""


class Unclear(Exception):
    """The databases a statement reaches cannot be determined; refused for non-admins."""


SYSTEM = frozenset({"pg_catalog", "information_schema"})   # their functions and types stay allowed
CATALOG_REFUSAL = ("reading the system catalogs is not allowed; use list_schemas, list_tables and describe_table "
                   "to see what exists")
PLAN_REFUSAL = "this query reads data outside the databases you can reach"

REG_TYPES = frozenset({"regclass", "regnamespace", "regproc", "regprocedure", "regoper", "regoperator", "regtype",
                       "regrole", "regcollation", "regconfig", "regdictionary"})
# functions that run SQL text, or change settings (search_path) for the rest of the statement
_RUNS_SQL = frozenset({"ts_stat", "ts_rewrite", "set_config", "query_to_xml", "query_to_xmlschema",
                       "query_to_xml_and_xmlschema", "cursor_to_xml", "cursor_to_xmlschema"})
_RUNS_SQL_SUFFIX = ("_to_xml", "_to_xmlschema", "_to_xml_and_xmlschema")   # table_/schema_/database_to_xml...
# functions that turn object ids into names or describe objects by id (pg_proc: reg*/oid arguments)
_BY_OID = frozenset({"acldefault", "makeaclitem", "obj_description", "col_description", "shobj_description",
                     "format_type", "row_security_active", "satisfies_hash_partition", "gin_clean_pending_list",
                     "amvalidate", "plpgsql_validator", "postgresql_fdw_validator"})
_BY_OID_PREFIX = ("binary_upgrade_", "brin_", "lo_", "dblink", "_pg_", "fmgr_")
_BY_OID_SUFFIX = ("sel", "joinsel", "_validator")
# pg_* functions that show nothing of any database (every other pg_* function is refused for non-admins)
PG_OK = frozenset({"pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_typeof", "pg_column_size", "pg_size_pretty",
                   "pg_size_bytes", "pg_backend_pid", "pg_client_encoding", "pg_input_is_valid",
                   "pg_input_error_info", "pg_get_serial_sequence", "pg_sequence_last_value",
                   "pg_advisory_xact_lock", "pg_advisory_xact_lock_shared", "pg_try_advisory_xact_lock",
                   "pg_try_advisory_xact_lock_shared"})
# functions whose first argument is a relation/type/function name (text resolved at run time)
_NAME_FIRST = frozenset({"nextval", "currval", "setval", "pg_get_serial_sequence", "pg_sequence_last_value",
                         "to_regclass", "to_regtype", "to_regtypemod", "to_regproc", "to_regprocedure",
                         "to_regoper", "to_regoperator", "to_regcollation", "to_regrole"}
                        | (REG_TYPES - {"regnamespace"}))
_SCHEMA_FIRST = frozenset({"to_regnamespace", "regnamespace"})   # the argument is a database name itself

_THREE_PART = {enums.ObjectType.OBJECT_COLUMN, enums.ObjectType.OBJECT_TRIGGER, enums.ObjectType.OBJECT_POLICY,
               enums.ObjectType.OBJECT_RULE, enums.ObjectType.OBJECT_TABCONSTRAINT}
_NAME_ATTRS = ("funcname", "defnames", "domainname", "conversion_name", "opclassname", "opfamilyname", "collname",
               "objname", "typeName", "name", "opclass", "collation", "dictname", "cfgname")

# statement kinds whose names are understood here; any other kind is Unclear
KNOWN_STATEMENTS = frozenset({
    "SelectStmt", "InsertStmt", "UpdateStmt", "DeleteStmt", "MergeStmt", "CreateStmt", "AlterTableStmt",
    "IndexStmt", "ViewStmt", "CreateTableAsStmt", "CreateSeqStmt", "AlterSeqStmt", "DropStmt", "TruncateStmt",
    "CommentStmt", "RenameStmt", "AlterObjectSchemaStmt", "AlterOwnerStmt", "GrantStmt",
    "AlterDefaultPrivilegesStmt", "CreateFunctionStmt", "AlterFunctionStmt", "DoStmt", "CallStmt",
    "CreateTrigStmt", "CreateEnumStmt", "AlterEnumStmt", "CompositeTypeStmt", "CreateDomainStmt",
    "AlterDomainStmt", "DefineStmt", "CreatePolicyStmt", "AlterPolicyStmt", "CreateStatsStmt", "AlterStatsStmt",
    "RuleStmt", "VariableSetStmt", "ReindexStmt", "ClusterStmt", "VacuumStmt", "RefreshMatViewStmt", "LockStmt",
    "ExplainStmt", "ConstraintsSetStmt", "TransactionStmt",
})

_DYNAMIC = ("dynamic SQL (EXECUTE) cannot be checked for the databases it reaches")


@dataclass
class Scan:
    schemas: set[str] = field(default_factory=set)
    catalog: bool = False            # reads a system catalog relation
    unclear: list[str] = field(default_factory=list)
    unparsable: bool = False


_IDENT = re.compile(r'\s*("(?:[^"]|"")+"|[^".\s()]+)\s*')


def _literal_name(text: str) -> list[str]:
    """`'crm.t'` / `'"Crm"."T"'` / `'crm.f(int)'` -> the dotted parts as Postgres reads them; Unclear if not a name."""
    parts, pos = [], 0
    while True:
        m = _IDENT.match(text, pos)
        if not m:
            raise Unclear("a name in a string could not be read")
        p = m.group(1)
        parts.append(p[1:-1].replace('""', '"') if p.startswith('"') else p.lower())
        pos = m.end()
        if pos >= len(text) or text[pos] == "(":
            return parts
        if text[pos] != ".":
            raise Unclear("a name in a string could not be read")
        pos += 1


def _strings(names) -> list[str] | None:
    if isinstance(names, tuple) and names and all(isinstance(x, ast.String) for x in names):
        return [x.sval for x in names]
    return None


def _const_text(node) -> str | None:
    if isinstance(node, ast.A_Const) and isinstance(node.val, ast.String):
        return node.val.sval
    return None


def _is_const(node) -> bool:
    return isinstance(node, ast.A_Const)


class _Collector:
    def __init__(self, scan: Scan):
        self.scan = scan
        self.cast_ok: set[int] = set()

    # ---- helpers
    def add(self, name) -> None:
        if isinstance(name, str) and name:
            self.scan.schemas.add(name)

    def unclear(self, why: str) -> None:
        self.scan.unclear.append(why)

    def qualified(self, names, offset: int = 2) -> None:
        parts = _strings(names)
        if parts and len(parts) >= offset:
            self.add(parts[-offset])

    def literal(self, node, *, is_schema: bool = False) -> None:
        text = _const_text(node)
        if text is None:
            self.unclear("a name given as an expression (not a string constant) cannot be checked")
            return
        try:
            parts = _literal_name(text)
        except Unclear as exc:
            self.unclear(str(exc))
            return
        if is_schema:
            self.add(parts[-1])
        elif len(parts) >= 2:
            self.add(parts[-2])

    def obj(self, objtype, obj) -> None:
        """The target of COMMENT / DROP / ALTER ... OWNER / SET SCHEMA / RENAME (names by object type)."""
        if isinstance(obj, ast.String):
            if objtype == enums.ObjectType.OBJECT_SCHEMA:
                self.add(obj.sval)
            return
        parts = _strings(obj)
        if not parts:
            return
        if objtype == enums.ObjectType.OBJECT_SCHEMA:
            for p in parts:
                self.add(p)
        elif objtype in _THREE_PART:
            if len(parts) >= 3:
                self.add(parts[-3])
        elif len(parts) >= 2:
            self.add(parts[-2])

    # ---- walk
    def walk(self, node) -> None:
        if isinstance(node, (tuple, list)):
            for x in node:
                self.walk(x)
            return
        if not isinstance(node, ast.Node):
            return
        handler = getattr(self, "on_" + type(node).__name__, None)
        if handler is not None:
            handler(node)
        self.generic(node)
        for member in node:
            self.walk(getattr(node, member))

    def generic(self, node) -> None:
        for attr in _NAME_ATTRS:
            value = getattr(node, attr, None)
            if isinstance(value, tuple):
                self.qualified(value)

    # ---- nodes
    def on_RangeVar(self, n):  # noqa: N802
        self.add(n.schemaname)
        if n.schemaname in SYSTEM or (n.schemaname or "").startswith("pg_") \
                or (n.schemaname is None and n.relname.startswith("pg_")):
            self.scan.catalog = True

    def on_TypeName(self, n):  # noqa: N802
        parts = _strings(n.names) or []
        if n.pct_type:              # schema.table.column%TYPE
            if len(parts) >= 3:
                self.add(parts[-3])
        elif len(parts) >= 2:
            self.add(parts[-2])
        if parts and parts[-1] in REG_TYPES and id(n) not in self.cast_ok:
            self.unclear(f"values of type {parts[-1]} cannot be checked for the databases they name")

    def on_TypeCast(self, n):  # noqa: N802
        parts = _strings(n.typeName.names) if n.typeName is not None else None
        if parts and parts[-1] in REG_TYPES:
            self.cast_ok.add(id(n.typeName))
            self.literal(n.arg, is_schema=parts[-1] == "regnamespace")

    def on_FuncCall(self, n):  # noqa: N802
        parts = _strings(n.funcname) or []
        if len(parts) >= 2:
            self.add(parts[-2])
        base = parts[-1].lower() if parts else ""
        args = n.args or ()
        if (base in _RUNS_SQL or base.endswith(_RUNS_SQL_SUFFIX) or base in _BY_OID
                or base.startswith(_BY_OID_PREFIX) or base.endswith(_BY_OID_SUFFIX)
                or (base.startswith("pg_") and base not in PG_OK)):
            self.unclear(f"{base}() is not allowed: it can show or read objects outside the databases you can reach")
        elif base in _NAME_FIRST or base in _SCHEMA_FIRST:
            if args:
                self.literal(args[0], is_schema=base in _SCHEMA_FIRST)
        elif base.startswith("has_") and base.endswith("_privilege"):
            if not all(_is_const(a) for a in args):
                self.unclear(f"{base}() with a name given as an expression cannot be checked")
            elif base == "has_schema_privilege":
                if args:
                    self.literal(args[0 if len(args) == 2 else 1], is_schema=True)
            else:
                for a in args:
                    text = _const_text(a)
                    if text is not None and "." in text:
                        self.literal(a)

    def on_ColumnRef(self, n):  # noqa: N802
        fields = n.fields or ()
        star = bool(fields) and isinstance(fields[-1], ast.A_Star)
        parts = _strings(fields[:-1] if star else fields) or []
        need = 2 if star else 3          # schema.table.* / schema.table.column
        if len(parts) >= need:
            self.add(parts[-need])

    def on_A_Expr(self, n):  # noqa: N802
        self.qualified(n.name)           # OPERATOR(crm.+)

    def on_DropStmt(self, n):  # noqa: N802
        for o in n.objects or ():
            self.obj(n.removeType, o)

    def on_CommentStmt(self, n):  # noqa: N802
        self.obj(n.objtype, n.object)

    def on_RenameStmt(self, n):  # noqa: N802
        self.obj(n.renameType, n.object)

    def on_AlterOwnerStmt(self, n):  # noqa: N802
        self.obj(n.objectType, n.object)

    def on_AlterObjectSchemaStmt(self, n):  # noqa: N802
        self.obj(n.objectType, n.object)
        self.add(n.newschema)

    def on_GrantStmt(self, n):  # noqa: N802
        if n.objtype == enums.ObjectType.OBJECT_SCHEMA or n.targtype == enums.GrantTargetType.ACL_TARGET_ALL_IN_SCHEMA:
            for o in n.objects or ():
                if isinstance(o, ast.String):
                    self.add(o.sval)

    def on_AlterDefaultPrivilegesStmt(self, n):  # noqa: N802
        for d in n.options or ():
            if isinstance(d, ast.DefElem) and d.defname == "schemas":
                for s in _strings(d.arg) or ():
                    self.add(s)

    def on_ReindexStmt(self, n):  # noqa: N802
        if n.kind == enums.ReindexObjectType.REINDEX_OBJECT_SCHEMA:
            self.add(n.name)
        elif n.kind != enums.ReindexObjectType.REINDEX_OBJECT_TABLE and n.kind != enums.ReindexObjectType.REINDEX_OBJECT_INDEX:
            self.unclear("REINDEX SYSTEM / DATABASE cannot be checked")

    def on_CreateSchemaStmt(self, n):  # noqa: N802
        self.add(n.schemaname)

    def on_DoStmt(self, n):  # noqa: N802
        self.body(n, _option(n.args, "language") or "plpgsql", _option(n.args, "as"))

    def on_CreateFunctionStmt(self, n):  # noqa: N802
        if n.sql_body is None:
            self.body(n, _option(n.options, "language") or "sql", _option(n.options, "as"))

    # ---- bodies
    def body(self, node, language: str, source) -> None:
        language = language.lower()
        if source is None:
            return
        if language == "sql":
            texts = [s.sval for s in (source if isinstance(source, tuple) else (source,)) if isinstance(s, ast.String)]
            for t in texts[:1]:           # AS 'body' (a second item is a C link symbol, not SQL)
                try:
                    stmts = pglast.parse_sql(t)
                except pglast.parser.ParseError as exc:
                    self.unclear(f"the function body cannot be checked: {exc}")
                    continue
                for raw in stmts:
                    self.statement(raw.stmt)
        elif language == "plpgsql":
            try:
                tree = pglast.parse_plpgsql(RawStream()(node))
            except Exception as exc:  # noqa: BLE001 - any failure to read the body is a refusal, not a crash
                self.unclear(f"the plpgsql body cannot be checked ({exc}); write qualified names in SQL statements "
                             "rather than in declarations")
                return
            self.plpgsql(tree)
        else:
            self.unclear(f"a body in language {language} cannot be checked")

    def plpgsql(self, tree) -> None:
        if isinstance(tree, list):
            for x in tree:
                self.plpgsql(x)
            return
        if not isinstance(tree, dict):
            return
        for key, value in tree.items():
            if key in ("PLpgSQL_stmt_dynexecute", "PLpgSQL_stmt_dynfors") or key == "dynquery":
                self.unclear(_DYNAMIC)
            elif key == "PLpgSQL_expr" and isinstance(value, dict):
                self.expression(value.get("query") or "", value.get("parseMode", 0))
            elif key == "PLpgSQL_type" and isinstance(value, dict):
                self.decl_type(value.get("typname") or "")
            self.plpgsql(value)

    def expression(self, query: str, mode: int) -> None:
        if mode in (3, 4, 5):                       # target := expression
            m = re.match(r"^(.*?)(:=|=)(.*)$", query, re.DOTALL)
            if m is None:
                self.unclear("a plpgsql assignment cannot be checked")
                return
            query = m.group(3)
        sql = query if mode == 0 else f"SELECT NULL::{query}" if mode == 1 else f"SELECT {query}"
        try:
            stmts = pglast.parse_sql(sql)
        except pglast.parser.ParseError as exc:
            self.unclear(f"a plpgsql expression cannot be checked: {exc}")
            return
        for raw in stmts:
            self.statement(raw.stmt)

    def decl_type(self, typname: str) -> None:
        name = re.sub(r"%(row)?type$", "", typname, flags=re.IGNORECASE)
        try:
            parts = _literal_name(name.split("(", 1)[0].split("[", 1)[0])
        except Unclear:
            self.unclear(f"the plpgsql declaration type {typname} cannot be checked")
            return
        offset = 3 if typname.lower().endswith("%type") else 2
        if len(parts) >= offset:
            self.add(parts[-offset])

    def statement(self, node) -> None:
        kind = type(node).__name__
        if kind not in KNOWN_STATEMENTS:
            self.unclear(f"{kind.removesuffix('Stmt')} statements cannot be checked for the databases they reach")
        self.walk(node)


def _option(options, name: str):
    for d in options or ():
        if isinstance(d, ast.DefElem) and d.defname == name:
            return d.arg.sval if name == "language" and isinstance(d.arg, ast.String) else d.arg
    return None


def scan(sql_or_node) -> Scan:
    """Names, catalog reads and unclear shapes of SQL text (or of one already parsed statement node).

    SQL that does not parse is unclear. Never raises for the SQL's content."""
    out = _Collector(Scan())
    if isinstance(sql_or_node, ast.Node):
        out.statement(sql_or_node)
        return out.scan
    try:
        stmts = pglast.parse_sql(sql_or_node)
    except pglast.parser.ParseError as exc:
        out.unclear(str(exc))           # the parser's own text ("syntax error at or near ...")
        out.scan.unparsable = True
        return out.scan
    for raw in stmts:
        out.statement(raw.stmt)
    return out.scan


def named_schemas(sql: str) -> set[str]:
    """Every database (schema) name the SQL names, `pg_catalog`/`information_schema` included.

    Raises Unclear when the names cannot all be determined (see the module docstring)."""
    s = scan(sql)
    if s.unclear:
        raise Unclear(s.unclear[0])
    return s.schemas


def catalog_names_leak(body: str) -> bool:
    """True when the SQL reads a system catalog relation (`pg_namespace`, `pg_class`, `pg_tables`, ...,
    `information_schema.*`), which would list database names; also True for SQL that does not parse."""
    s = scan(body)
    return s.catalog or s.unparsable


def text_problem(sql_or_node, allowed, *, is_admin: bool) -> str | None:
    """Why this SQL may not run for a user who can reach `allowed` databases, or None. Admins: always None.

    A named database outside `allowed` gives the unknown-database text, whether it exists or not."""
    if is_admin:
        return None
    s = scan(sql_or_node)
    for name in sorted(s.schemas - SYSTEM):
        if name not in allowed:
            return introspect.unknown_schema_message(name)
    if s.unclear:
        return s.unclear[0]
    if s.catalog:
        return CATALOG_REFUSAL
    return None


def check_text(body: str, allowed, *, is_admin: bool) -> None:
    """`text_problem` as a QueryError."""
    problem = text_problem(body, allowed, is_admin=is_admin)
    if problem:
        raise QueryError(problem)


def reach_problem(user, sql_or_node, level: str = "editor") -> tuple[str, str] | None:
    """For SQL that will run as the read/write role (raw SQL, view queries): `(code, message)` when it names a
    database where `user` lacks `level`, reads the catalogs, or cannot be checked; None when fine. Admins: None.

    code "invalid" with the unknown-database text for a database they cannot reach (hidden equals missing: the
    same text whether it exists or not), "forbidden" with the role text for one they can read but not at `level`.
    """
    from accounts import access

    if access.is_admin(user):
        return None
    s = scan(sql_or_node)
    for name in sorted(s.schemas - SYSTEM):
        try:
            access.require(user, name, level)
        except introspect.UnknownSchema as exc:
            return "invalid", f"{exc} Call list_schemas to see the databases."
        except access.AccessDenied as exc:
            return "forbidden", str(exc)
    if s.unclear:
        return "invalid", f"{s.unclear[0]}; only an admin can propose it"
    if s.catalog:
        return "invalid", CATALOG_REFUSAL
    return None


def view_queries(ops):
    """`(where, query)` for each create_view operation's query (the SQL a change carries besides raw SQL)."""
    if not isinstance(ops, list):
        return []
    return [(f"operation {i + 1} (create_view)", o["query"]) for i, o in enumerate(ops)
            if isinstance(o, dict) and o.get("op") == "create_view" and isinstance(o.get("query"), str)]


def _plan_nodes(tree):
    if isinstance(tree, list):
        for x in tree:
            yield from _plan_nodes(x)
    elif isinstance(tree, dict):
        yield tree
        for v in tree.values():
            if isinstance(v, (list, dict)):
                yield from _plan_nodes(v)


def plan_problem(plan, allowed) -> str | None:
    """Why this EXPLAIN (FORMAT JSON) plan reads outside `allowed`, or None. Walks every node of the tree."""
    for node in _plan_nodes(plan):
        if "Relation Name" in node:
            schema = node.get("Schema")
            if schema not in allowed:
                return CATALOG_REFUSAL if schema in SYSTEM or (schema or "").startswith("pg_") else PLAN_REFUSAL
        elif node.get("Node Type") in ("Foreign Scan", "Custom Scan"):
            return PLAN_REFUSAL                    # a scan without a named relation: cannot be checked
        if "Function Name" in node and node.get("Schema") not in set(allowed) | {"pg_catalog"}:
            return PLAN_REFUSAL
    return None


def check_plan(conn, body: str, allowed, *, is_admin: bool) -> None:
    """Plan `body` on `conn` (search_path and timeout already set) and raise QueryError if any relation of the
    plan is outside `allowed`. Admins reach everything: nothing is planned for them."""
    if is_admin:
        return
    row = conn.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + body).fetchone()
    value = next(iter(row.values())) if isinstance(row, dict) else row[0]
    plan = json.loads(value) if isinstance(value, str) else value
    problem = plan_problem(plan, set(allowed))
    if problem:
        raise QueryError(problem)
