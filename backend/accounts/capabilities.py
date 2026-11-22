"""What a person's role lets them do, for the profile page: the MCP tools (from the same table the MCP server
uses to hide tools) and a short text per role."""
import logging

from mcp_server import tools

logger = logging.getLogger("accounts")

ROLE_SUMMARY = [
    "Read data and the schema",
    "Edit data, import files, propose schema changes",
    "Approve changes, settings, backups",
    "Manage users, roles and who can reach each database",
]


_warned = False


def _descriptions() -> dict[str, str]:
    """The server's own tool texts. Uses FastMCP's private tool manager, so any failure gives no texts (logged
    once) instead of breaking /api/me."""
    global _warned
    try:
        from mcp_server.app import mcp  # imported late because it starts Django set-up

        return {t.name: t.description or "" for t in mcp._tool_manager.list_tools()}
    except Exception:
        if not _warned:
            _warned = True
            logger.warning("could not read MCP tool descriptions; the profile shows tools without them", exc_info=True)
        return {}


def build(user) -> dict:
    texts = _descriptions()
    return {
        "mcpTools": [
            {"name": name, "minRole": need, "allowed": tools.allowed(user, name), "description": texts.get(name, "")}
            for name, need in tools.TOOL_ROLES.items()
        ],
        "roleSummary": list(ROLE_SUMMARY),
    }
