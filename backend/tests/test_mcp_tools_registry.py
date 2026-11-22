"""Guards the tool registry: a tool cannot be half-added."""
import asyncio

from chat import toolspecs
from mcp_server import tools

# Not offered to the web chat assistant (chat.toolspecs.NOT_IN_CHAT): the tools that apply or decide a change (in the
# web app a person does that; revert_change proposes and approves its own reverse in one call), and the MCP-only
# import helpers (in the web app a file goes through the Import tab), and the admin's grant management (in the web
# app an admin uses the Users page; the chat never grants access).
NOT_IN_CHAT = toolspecs.NOT_IN_CHAT


def test_every_tool_has_a_role_and_an_implementation():
    assert set(tools.TOOL_ROLES) == set(tools.IMPL)


def test_every_tool_is_registered_with_the_mcp_server():
    from mcp_server.app import mcp
    registered = {t.name for t in asyncio.run(super(type(mcp), mcp).list_tools())}
    assert registered == set(tools.TOOL_ROLES)


def test_the_web_chat_has_every_tool_except_deciding():
    assert NOT_IN_CHAT == {"approve_change", "reject_change", "revert_change", "match_data", "get_import_recipe",
                           "get_user_access", "set_user_access"}
    assert set(toolspecs.SPECS) == set(tools.TOOL_ROLES) - NOT_IN_CHAT


def test_import_helpers_are_editor_tools():
    assert {t: tools.TOOL_ROLES[t] for t in ("match_data", "get_import_recipe")} == {
        "match_data": "editor", "get_import_recipe": "editor"}


def test_clamp():
    assert tools.clamp(None, 20, 100) == 20
    assert tools.clamp(0, 20, 100) == 20
    assert tools.clamp(-5, 20, 100) == 20
    assert tools.clamp("abc", 20, 100) == 20
    assert tools.clamp(True, 20, 100) == 20
    assert tools.clamp(7, 20, 100) == 7
    assert tools.clamp(10**9, 20, 100) == 100


def test_minimum_roles_of_the_reach_tools_are_pinned():
    """Spelled out on purpose: a wrong role here is an authorisation bug the registry tests cannot see."""
    assert {t: tools.TOOL_ROLES[t] for t in (
        "list_changes", "get_audit_log", "revert_change", "recheck_change", "read_rows", "find_duplicates",
        "list_duplicates", "get_duplicate", "merge_duplicates", "dismiss_duplicate", "export_schema", "reindex_search",
    )} == {
        "list_changes": "viewer", "get_audit_log": "approver", "revert_change": "approver", "recheck_change": "editor",
        "read_rows": "viewer", "find_duplicates": "editor", "list_duplicates": "viewer", "get_duplicate": "viewer",
        "merge_duplicates": "editor", "dismiss_duplicate": "editor", "export_schema": "viewer",
        "reindex_search": "approver",
    }


def test_load_tools_are_editor_tools():
    for t in ("begin_load", "append_rows", "finish_load", "get_load", "cancel_load"):
        assert tools.TOOL_ROLES[t] == "editor"


def test_minimum_roles_of_the_parity_tools_are_pinned():
    assert {t: tools.TOOL_ROLES[t] for t in (
        "get_rule_history", "reopen_duplicate", "create_backup", "list_backups", "profile_column", "lookup_options",
    )} == {
        "get_rule_history": "viewer", "reopen_duplicate": "editor", "create_backup": "approver",
        "list_backups": "approver", "profile_column": "viewer", "lookup_options": "viewer",
    }


def test_access_tools_are_admin_tools():
    """Granting database access is an admin's decision: no other token may even list these tools."""
    assert {t: tools.TOOL_ROLES[t] for t in ("get_user_access", "set_user_access")} == {
        "get_user_access": "admin", "set_user_access": "admin"}
    assert {t for t, need in tools.TOOL_ROLES.items() if need == "admin"} == {"get_user_access", "set_user_access"}
