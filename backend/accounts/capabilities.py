"""What a person's role lets them do, for the profile page: the MCP tools (from the same table the MCP server
uses to hide tools) and a short text per role."""
from mcp_server import tools

ROLE_SUMMARY = [
    "Read data and the schema",
    "Edit data, import files, propose schema changes",
    "Approve changes, settings, backups",
    "Manage users and roles",
]


def _descriptions() -> dict[str, str]:
    from mcp_server.app import mcp  # the server's own tool texts; imported late because it starts Django set-up

    return {t.name: t.description or "" for t in mcp._tool_manager.list_tools()}


def build(user) -> dict:
    texts = _descriptions()
    return {
        "mcpTools": [
            {"name": name, "minRole": need, "allowed": tools.allowed(user, name), "description": texts.get(name, "")}
            for name, need in tools.TOOL_ROLES.items()
        ],
        "roleSummary": list(ROLE_SUMMARY),
    }
