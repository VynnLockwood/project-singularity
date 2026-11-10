"""Raw SQL for kind-`sql` changes: parse, refuse, warn, dry-run, run (spec 2026-11-10 section 4).

The database role is the real fence (no rights on the `singularity` schema, not a superuser); the refusals here
are defence in depth and give the AI a message it can act on.
"""
from dataclasses import dataclass

import pglast
import psycopg
from pglast import ast, enums
from pglast.visitors import Visitor
from psycopg import sql as psql

from schema import executor, introspect
from schema.conn import rw_connection


class RawSqlError(Exception):
    """A problem with the SQL the caller can act on."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Statement:
    text: str
    number: int        # 1-based
    node: object       # the pglast node


_ROLES = ("role", "session_authorization")
_OK_LANGUAGES = ("sql", "plpgsql")
_NODE_REFUSALS = {
    ast.TransactionStmt: "transaction control (the whole script already runs in one transaction)",
    ast.CopyStmt: "COPY (use insert_rows, or load a script)",
    ast.CreatedbStmt: "CREATE DATABASE",
    ast.DropdbStmt: "DROP DATABASE",
    ast.AlterSystemStmt: "ALTER SYSTEM",
    ast.ListenStmt: "LISTEN",
    ast.UnlistenStmt: "UNLISTEN",
    ast.NotifyStmt: "NOTIFY",
    ast.LoadStmt: "LOAD",
    ast.CreateSchemaStmt: "CREATE SCHEMA (use the create_schema operation in propose_change)",
    ast.CreateExtensionStmt: "CREATE EXTENSION (the database role cannot install extensions)",
    ast.CreateRoleStmt: "role changes",
    ast.AlterRoleStmt: "role changes",
    ast.DropRoleStmt: "role changes",
    ast.GrantRoleStmt: "role membership changes (GRANT of privileges on objects is allowed)",
}
_SCHEMA_HINT = "{} on a schema (use the create_schema / rename_schema / drop_schema operations in propose_change)"


def _lang(options) -> str | None:
    for d in options or ():
        if d.defname == "language" and isinstance(d.arg, ast.String):
            return d.arg.sval.lower()
    return None


def _refusal(node) -> str | None:
    """Why this statement is refused, or None."""
    for cls, why in _NODE_REFUSALS.items():
        if isinstance(node, cls):
            return why
    if isinstance(node, ast.VariableSetStmt) and node.name in _ROLES:
        return "changing the role or session authorization"
    if isinstance(node, ast.VacuumStmt) and node.is_vacuumcmd:
        return "VACUUM"
    if isinstance(node, ast.DoStmt) and (_lang(node.args) or "plpgsql") not in _OK_LANGUAGES:
        return f"DO in language {_lang(node.args)} (only sql and plpgsql)"
    if isinstance(node, ast.CreateFunctionStmt) and (_lang(node.options) or "sql") not in _OK_LANGUAGES:
        return f"functions in language {_lang(node.options)} (only sql and plpgsql)"
    if isinstance(node, (ast.DropStmt, ast.RenameStmt, ast.AlterOwnerStmt)):
        kind = (getattr(node, "removeType", None) or getattr(node, "renameType", None)
                or getattr(node, "objectType", None))
        if kind == enums.ObjectType.OBJECT_SCHEMA:
            return _SCHEMA_HINT.format("DROP/ALTER")
    return None


def _concurrent(node) -> bool:
    if isinstance(node, (ast.IndexStmt, ast.DropStmt)):
        return bool(node.concurrent)
    if isinstance(node, ast.ReindexStmt):
        return any(d.defname == "concurrently" for d in node.params or ())
    return False


class _SetConfig(Visitor):
    found = False

    def visit_FuncCall(self, ancestors, node):  # noqa: N802 - pglast visitor naming
        names = [x.sval for x in node.funcname if isinstance(x, ast.String)]
        if names and names[-1] == "set_config":
            self.found = True


def parse_statements(sql: str, *, max_bytes: int, max_statements: int) -> list[Statement]:
    """Split `sql` into checked statements. Raises RawSqlError with a message the AI can act on."""
    raw = sql.encode()
    if len(raw) > max_bytes:
        raise RawSqlError(f"the SQL is {len(raw)} bytes; the most allowed is {max_bytes} (setting limits.maxSqlBytes)")
    try:
        stmts = pglast.parse_sql(sql)
    except pglast.parser.ParseError as exc:
        raise RawSqlError(f"syntax error: {exc} (position is in characters from the start of the SQL)") from exc
    if not stmts:
        raise RawSqlError("no statements to run")
    if len(stmts) > max_statements:
        raise RawSqlError(f"{len(stmts)} statements; the most allowed is {max_statements} (setting limits.maxSqlStatements)")
    out = []
    for n, raw_stmt in enumerate(stmts, 1):
        start = raw_stmt.stmt_location
        end = start + raw_stmt.stmt_len if raw_stmt.stmt_len else len(sql)   # pglast offsets are characters
        text = sql[start:end].strip().rstrip(";").strip()
        node = raw_stmt.stmt
        why = _refusal(node)
        if why:
            raise RawSqlError(f"statement {n}: {why} is not allowed in raw SQL")
        if _concurrent(node):
            raise RawSqlError(f"statement {n}: CONCURRENTLY is not allowed in raw SQL: it runs inside one "
                              "transaction, so concurrent index work is not possible")
        v = _SetConfig()
        v(node)
        if v.found:
            raise RawSqlError(f"statement {n}: set_config() is not allowed in raw SQL (it could change the role)")
        out.append(Statement(text, n, node))
    return out


class _Targets(Visitor):
    """Table names a statement mentions, as (schema or None, name), minus CTE names."""

    def __init__(self):
        super().__init__()
        self.ctes: set[str] = set()
        self.tables: list[tuple[str | None, str]] = []

    def visit_CommonTableExpr(self, ancestors, node):  # noqa: N802
        self.ctes.add(node.ctename)

    def visit_RangeVar(self, ancestors, node):  # noqa: N802
        self.tables.append((node.schemaname, node.relname))


def _v(message: str) -> dict:
    return {"rule": "rawSql", "severity": "warn", "message": message, "op_index": None, "suggestion": None}


def warnings_for(statements: list[Statement], schema: str) -> list[dict]:
    """Warnings about what the statements do (the standing 'rules were not checked' one is added by the service)."""
    out = []
    for st in statements:
        n, node = st.number, st.node
        if isinstance(node, ast.DropStmt):
            out.append(_v(f"statement {n} drops objects"))
        if isinstance(node, ast.TruncateStmt):
            out.append(_v(f"statement {n} truncates (empties) tables"))
        if isinstance(node, ast.DeleteStmt) and node.whereClause is None:
            out.append(_v(f"statement {n} is a DELETE without a WHERE: it removes every row"))
        t = _Targets()
        t(node)
        seen = set()
        for sch, name in t.tables:
            if sch is None and name in t.ctes:
                continue
            if sch is None and name not in seen:
                seen.add(name)
                out.append(_v(f"statement {n} refers to {name} without a schema; it resolves in {schema}"))
            elif sch not in (None, schema, "pg_catalog", "information_schema") and (sch, name) not in seen:
                seen.add((sch, name))
                out.append(_v(f"statement {n} touches schema {sch}, not the chosen database {schema}"))
    return out


def _tag(cur) -> str:
    return cur.statusmessage or "OK"


def _run(statements: list[Statement], schema: str, *, commit: bool) -> list[str]:
    lines: list[str] = []
    try:
        with rw_connection() as conn:
            conn.execute(psql.SQL("SET LOCAL lock_timeout = {}").format(psql.Literal(executor.LOCK_TIMEOUT)))
            conn.execute(psql.SQL("SET LOCAL statement_timeout = {}").format(psql.Literal(executor.STATEMENT_TIMEOUT)))
            conn.execute(psql.SQL("SET LOCAL search_path TO {}").format(psql.Identifier(schema)))
            seqs: list[dict] = []
            if not commit:
                # a dry run must not move sequences: hold writers off the tables that own one, snapshot, restore
                tables = [r["name"] for r in introspect.list_tables(schema)]
                for t in tables:
                    owned = executor._sequence_states(conn, [t], schema)
                    if owned:
                        conn.execute(psql.SQL("LOCK TABLE {}.{} IN SHARE ROW EXCLUSIVE MODE").format(
                            psql.Identifier(schema), psql.Identifier(t)))
                        seqs += owned
                conn.execute("SAVEPOINT raw_dry_run")
            n = 0
            try:
                for st in statements:
                    n = st.number
                    lines.append(f"{_tag(conn.execute(st.text))} (statement {n})")
            except BaseException:
                if not commit:
                    conn.execute("ROLLBACK TO SAVEPOINT raw_dry_run")
                    executor._restore_sequences(conn, seqs)
                raise
            if commit:
                conn.commit()
            else:
                executor._restore_sequences(conn, seqs)
                conn.rollback()
    except psycopg.Error as exc:
        msg = exc.diag.message_primary or str(exc)
        raise RawSqlError(f"statement {n or 1}: {msg}") from exc
    return lines


def dry_run(statements: list[Statement], schema: str) -> list[str]:
    """Run every statement as the read/write role, then roll back; one diff line per statement."""
    return _run(statements, schema, commit=False)


def run_in_transaction(statements: list[Statement], schema: str) -> list[str]:
    """Run every statement in one transaction and commit; any error rolls everything back."""
    return _run(statements, schema, commit=True)
