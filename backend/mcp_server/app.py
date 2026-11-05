"""The MCP server (spec section 3): `uvicorn mcp_server.app:app`.

Auth: every request carries `Authorization: Bearer sgl_...` (a personal access
token made in the web app). The token is looked up on every request, so a role
change or a revoked token takes effect immediately. Each user only sees the
tools their role allows, and the tool functions re-check the role anyway.

Stateless HTTP mode: no server-side session, so a request is always handled
as the user whose token it carries.
"""
import os
from contextvars import ContextVar

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from asgiref.sync import sync_to_async  # noqa: E402
from django.db import close_old_connections  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

from accounts import tokens  # noqa: E402

from . import tools  # noqa: E402

current_user: ContextVar = ContextVar("current_user", default=None)

INSTRUCTIONS = """\
Singularity manages a master-data PostgreSQL database. Start with get_rules and list_tables; use search_schema to find where a kind of data belongs.
You never write SQL to change anything: call propose_change with structured operations (create_table,
add_column, insert_rows, ...). The gate checks your proposal against the team's rules and returns a
diff. Show the diff to the user and ask for confirmation before calling approve_change. Schema changes
by editors wait for an approver. Dropping things needs a 'reason'. Use query for read-only SELECTs.
"""


class SingularityMCP(MCPServer):
    """Hides tools above the caller's role, and refuses them if called anyway."""

    async def list_tools(self):
        user = current_user.get()
        return [t for t in await super().list_tools() if tools.allowed(user, t.name)]

    async def call_tool(self, name, arguments, context=None):
        if not tools.allowed(current_user.get(), name):
            raise ToolError(f"tool {name!r} is not available for your role")
        return await super().call_tool(name, arguments, context)


async def run(tool: str, **kwargs):
    """Run a (synchronous, ORM-using) tool function off the event loop, as the calling user."""
    user = current_user.get()
    if not tools.allowed(user, tool):
        raise ToolError(f"tool {tool!r} is not available for your role")

    def call():
        close_old_connections()
        try:
            return tools.IMPL[tool](user, **kwargs)
        finally:
            close_old_connections()

    try:
        return await sync_to_async(call, thread_sensitive=False)()
    except tools.ToolFailure as exc:
        raise ToolError(str(exc)) from exc


def _hosts() -> list[str]:
    raw = os.environ.get("MCP_ALLOWED_HOSTS") or os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost")
    hosts = []
    for h in (x.strip() for x in raw.split(",") if x.strip()):
        hosts += [h, f"{h}:*"]
    return hosts


mcp = SingularityMCP("Singularity", instructions=INSTRUCTIONS)


@mcp.tool(name="get_rules", description="The team's database design rules. Read these before designing tables.")
async def get_rules() -> dict:
    return await run("get_rules")


@mcp.tool(name="list_tables", description="Tables in the master data database, with comments and row estimates.")
async def list_tables() -> dict:
    return await run("list_tables")


@mcp.tool(name="describe_table", description="Columns, types, constraints and indexes of one table.")
async def describe_table(table: str) -> dict:
    return await run("describe_table", table=table)


@mcp.tool(name="query", description="Run one read-only SELECT against master data. Row-limited; cannot change anything.")
async def query(sql: str, limit: int | None = None) -> dict:
    return await run("query", sql=sql, limit=limit)


@mcp.tool(
    name="search_schema",
    description=(
        "Find where a kind of data belongs: semantic search over tables and columns (Thai or English). "
        "Use before proposing a new column, to avoid duplicates. If 'confident' is false, nothing fits well."
    ),
)
async def search_schema(query: str, limit: int | None = None) -> dict:
    return await run("search_schema", query=query, limit=limit)


@mcp.tool(
    name="propose_change",
    description=(
        "Propose structural or data changes as a list of operations (never SQL). Each is an object with an 'op': "
        "create_table{name,columns[{name,type,nullable?,unique?,default?,comment?,references?{table,column?}}],comment?}, "
        "add_column{table,column}, rename_table{table,new_name}, rename_column{table,column,new_name}, "
        "alter_column_type{table,column,type}, set_column_comment{table,column,comment}, add_index{table,columns,unique?}, "
        "add_unique{table,columns}, add_foreign_key{table,column,ref_table,ref_column?}, "
        "drop_column{table,column,reason}, drop_table{table,reason}, drop_index{name,reason}, drop_constraint{table,name,reason}, "
        "insert_rows{table,rows[]}, update_rows{table,where{},set{}}, deactivate_rows{table,where{}}. "
        "Types: text, integer, bigint, smallint, boolean, date, timestamptz, timestamp, uuid, jsonb, real, "
        "double precision, numeric, numeric(p,s), varchar(n). Audit columns are added automatically. "
        "Returns the checked change with a diff; nothing is applied until it is approved."
    ),
)
async def propose_change(operations: list[dict], prompt: str = "") -> dict:
    return await run("propose_change", operations=operations, prompt=prompt)


@mcp.tool(name="get_change", description="Status, diff and any errors of a proposed change.")
async def get_change(change_id: int) -> dict:
    return await run("get_change", change_id=change_id)


@mcp.tool(
    name="approve_change",
    description=(
        "Approve and apply a pending change. ONLY call this after the user has seen the diff and explicitly "
        "confirmed. Editors can approve their own data changes; schema changes need an approver."
    ),
)
async def approve_change(change_id: int) -> dict:
    return await run("approve_change", change_id=change_id)


@mcp.tool(name="reject_change", description="Withdraw or decline a pending change, with an optional note.")
async def reject_change(change_id: int, note: str = "") -> dict:
    return await run("reject_change", change_id=change_id, note=note)


@mcp.tool(name="list_pending", description="Pending changes waiting for a decision (approvers see the whole queue).")
async def list_pending() -> dict:
    return await run("list_pending")


class BearerAuth:
    """ASGI middleware: authenticate the token, publish the user for this request only."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] == "/healthz":
            return await JSONResponse({"ok": True})(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        raw = auth[7:].strip() if auth.lower().startswith("bearer ") else ""

        def lookup():
            close_old_connections()
            try:
                return tokens.authenticate(raw)
            finally:
                close_old_connections()

        user = await sync_to_async(lookup, thread_sensitive=False)()
        if user is None:
            resp = JSONResponse(
                {"error": "missing or invalid token"}, status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="singularity"'},
            )
            return await resp(scope, receive, send)

        marker = current_user.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            current_user.reset(marker)


def build_app():
    """A fresh ASGI app. The SDK's session manager can only be started once per instance,
    so tests build one per server; production uses the module-level `app`."""
    return BearerAuth(
        mcp.streamable_http_app(
            stateless_http=True,
            host="0.0.0.0",
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True, allowed_hosts=_hosts(), allowed_origins=[],
            ),
        )
    )


app = build_app()
