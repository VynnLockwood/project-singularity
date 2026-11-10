"""The MCP server (spec section 3): `uvicorn mcp_server.app:app`.

Auth: every request carries `Authorization: Bearer sgl_...` (a personal access
token made in the web app), or the same token in the URL as /mcp/t/sgl_... . The token is looked up on every request, so a role
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
Settings, gate rules and people's roles are changed the same way: read them with get_settings (and list_users),
then call propose_config_change with structured operations (set_setting, reset_setting, update_rule,
create_rule, set_user_role). These always wait in the approval queue for an approver; never claim they are applied.
To load a whole pg_dump-style SQL script, call submit_script with the file contents inline (small scripts only;
big ones are uploaded in the web app, then validate_script with the script_id). The script is parsed into the same
gated operations — never send raw SQL to any other tool.
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


DB_NOTE = "A 'database' is a Postgres schema; `schema` defaults to master. Call list_schemas to see them."


@mcp.tool(name="list_schemas", description="The databases (Postgres schemas) you can read and change, with table counts and comments. master is the default.")
async def list_schemas() -> dict:
    return await run("list_schemas")


@mcp.tool(name="list_tables", description="Tables in one database (schema, default master), with comments and row estimates. " + DB_NOTE)
async def list_tables(schema: str = "master") -> dict:
    return await run("list_tables", schema=schema)


@mcp.tool(name="describe_table", description="Columns, types, constraints and indexes of one table. " + DB_NOTE)
async def describe_table(table: str, schema: str = "master") -> dict:
    return await run("describe_table", table=table, schema=schema)


@mcp.tool(name="query", description="Run one read-only SELECT against one database (schema, default master). Row-limited; cannot change anything. " + DB_NOTE)
async def query(sql: str, limit: int | None = None, schema: str = "master") -> dict:
    return await run("query", sql=sql, limit=limit, schema=schema)


@mcp.tool(
    name="search_schema",
    description=(
        "Find where a kind of data belongs: semantic search over tables and columns (Thai or English). "
        "Use before proposing a new column, to avoid duplicates. If 'confident' is false, nothing fits well. "
        "Searches one database (schema, default master); pass schema='*' to search all of them."
    ),
)
async def search_schema(query: str, limit: int | None = None, schema: str = "master") -> dict:
    return await run("search_schema", query=query, limit=limit, schema=schema)


@mcp.tool(
    name="propose_change",
    description=(
        "Propose structural or data changes as a list of operations (never SQL). Each is an object with an 'op': "
        "create_table{name,columns[{name,type,nullable?,unique?,default?,comment?,references?{table,column?}}],comment?}, "
        "add_column{table,column}, rename_table{table,new_name}, rename_column{table,column,new_name}, "
        "alter_column_type{table,column,type}, set_column_comment{table,column,comment}, add_index{table,columns,unique?}, "
        "add_unique{table,columns}, add_foreign_key{table,column,ref_table,ref_column?,on_delete?(restrict|cascade|set null|no action)}, "
        "add_check{table,name,expression}, set_table_comment{table,comment}, "
        "drop_column{table,column,reason}, drop_table{table,reason}, drop_index{name,reason}, drop_constraint{table,name,reason}, "
        "insert_rows{table,rows[]}, update_rows{table,where{},set{}}, deactivate_rows{table,where{}}. "
        "Schema operations: create_schema{name,comment?}, rename_schema{schema,new_name}, drop_schema{schema,reason}. "
        "Schema ops go alone, in their own change; a 'database' is a Postgres schema. Other operations act on the `schema` argument (default master). "
        "(bulk_insert exists but is produced by script intake only; never write it yourself, the gate refuses it.) "
        "Types: text, integer, bigint, smallint, boolean, date, timestamptz, timestamp, uuid, jsonb, real, "
        "double precision, numeric, numeric(p,s), varchar(n). Audit columns are added automatically. "
        "Returns the checked change with a diff; nothing is applied until it is approved."
    ),
)
async def propose_change(operations: list[dict], prompt: str = "", schema: str = "master") -> dict:
    return await run("propose_change", operations=operations, prompt=prompt, schema=schema)


@mcp.tool(
    name="get_settings",
    description="The quality settings (search confidence, duplicate similarity, limits, chat model...) with ranges, and every gate rule including switched-off ones.",
)
async def get_settings() -> dict:
    return await run("get_settings")


@mcp.tool(
    name="propose_config_change",
    description=(
        "Propose a change to settings, gate rules or people's roles. These are important, so the change NEVER applies on its own: "
        "it is checked, shown with a diff, and waits in the approval queue for an approver (live in their web app). "
        "%s Returns the checked change; tell the user it is waiting as change #id. Only an approver can approve it."
    ) % """Configuration operations (settings, rules, roles). Each is an object with an 'op': set_setting{key,value}, reset_setting{key}, update_rule{name,severity?('block'|'warn'),enabled?,params?,description?}, create_rule{name,type,params,severity?,enabled?,description?}, set_user_role{email,role('viewer'|'editor'|'approver')}. Call get_settings first for the keys, allowed ranges and current rules. Never mix these with table or data operations.""",
)
async def propose_config_change(operations: list[dict], prompt: str = "") -> dict:
    return await run("propose_config_change", operations=operations, prompt=prompt)


@mcp.tool(name="list_users", description="People who can sign in, with their roles (approvers only).")
async def list_users() -> dict:
    return await run("list_users")


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


@mcp.tool(
    name="submit_script",
    description=(
        "Upload a small pg_dump-style SQL file inline (text, <= maxInlineScriptBytes; check get_settings for the cap), "
        "validate it against the gate and submit it as one approvable Change. No raw SQL is executed: the script is "
        "parsed into gated operations. For bigger files, tell the user to upload the file in the web app, then use "
        "validate_script with the script_id. Optional mapping: {tables: {src_table: {pk: ', srcname: table}}, "
        "columns: {...}}; leave out to let the system suggest one. The script loads into one database (schema, default master)."
    ),
)
async def submit_script(name: str, content: str, mapping: dict | None = None, schema: str = "master") -> dict:
    return await run("submit_script", name=name, content=content, mapping=mapping, schema=schema)


@mcp.tool(
    name="validate_script",
    description=(
        "Re-validate a previously uploaded script and return the trimmed report "
        "(errors with line numbers, ignored statements, ops summary, gate result, orphan samples, dry-run rows). "
        "Returns the same report as at upload, so re-validation is idempotent."
    ),
)
async def validate_script(script_id: int, mapping: dict | None = None, schema: str = "master") -> dict:
    return await run("validate_script", script_id=script_id, mapping=mapping, schema=schema)


@mcp.tool(
    name="get_script",
    description="Status, stored report and change link of an uploaded/validated/submitted script.",
)
async def get_script(script_id: int) -> dict:
    return await run("get_script", script_id=script_id)


@mcp.tool(name="list_changes", description=tools.DESCRIPTIONS["list_changes"])
async def list_changes(status: str | None = None, kind: str | None = None, schema: str | None = None,
                       mine: bool = False, limit: int | None = None) -> dict:
    return await run("list_changes", status=status, kind=kind, schema=schema, mine=mine, limit=limit)


@mcp.tool(name="get_audit_log", description=tools.DESCRIPTIONS["get_audit_log"])
async def get_audit_log(action: str | None = None, actor: str | None = None, limit: int | None = None) -> dict:
    return await run("get_audit_log", action=action, actor=actor, limit=limit)


LINK_PREFIX = "/mcp/t/"


class BearerAuth:
    """ASGI middleware: authenticate the token, publish the user for this request only."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] == "/healthz" or scope["path"] == "/mcp/healthz":
            return await JSONResponse({"ok": True, "tools": len(tools.IMPL)})(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        raw = auth[7:].strip() if auth.lower().startswith("bearer ") else ""

        # Connection link: /mcp/t/<token> carries the token in the URL, for clients that cannot send
        # a header (Claude.ai and Gemini custom connectors). The path is rewritten to /mcp.
        path = scope["path"]
        if path.startswith(LINK_PREFIX):
            raw = raw or path[len(LINK_PREFIX):].strip("/")
            scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}

        def lookup():
            close_old_connections()
            try:
                return tokens.authenticate(raw)
            finally:
                close_old_connections()

        user = await sync_to_async(lookup, thread_sensitive=False)()
        if user is None:
            # points OAuth clients (Claude.ai, ...) at the discovery document, RFC 9728
            base = f"{headers.get('x-forwarded-proto', 'http')}://{headers.get('host', 'localhost')}"
            resp = JSONResponse(
                {"error": "missing or invalid token"}, status_code=401,
                headers={"WWW-Authenticate": f'Bearer realm="singularity", resource_metadata="{base}/.well-known/oauth-protected-resource"'},
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
