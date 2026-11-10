"""Raw SQL for kind-`sql` changes: parse, refuse, warn, dry-run, run (spec 2026-11-10 section 4).

The database role is the real fence (no rights on the `singularity` schema, not a superuser); the refusals here
are defence in depth and give the AI a message it can act on.
"""
from dataclasses import dataclass

import pglast
from pglast import ast, enums
from pglast.visitors import Visitor


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
