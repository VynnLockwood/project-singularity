"""Every web `api/` route is either reachable through an MCP tool or says why only a person does it.

Adding a route without an entry in COVERED or WEB_ONLY fails here, so the MCP server cannot silently fall behind
the web app. The reasons in WEB_ONLY are the contract: things that must not be an AI's to do, or that are about
the browser session itself.
"""
from django.urls import URLPattern, URLResolver, get_resolver

from mcp_server import tools

COVERED = {
    "api/changes": ["list_changes", "propose_change", "propose_sql"],
    "api/changes/<int:change_id>": ["get_change"],
    "api/changes/<int:change_id>/<str:action>": ["approve_change", "reject_change", "revert_change", "recheck_change"],
    "api/settings": ["get_settings", "propose_config_change"],
    "api/rules": ["get_rules", "propose_config_change"],
    "api/rules/<int:rule_id>": ["get_rules", "propose_config_change"],
    "api/rules/<int:rule_id>/versions": ["get_rule_history"],
    "api/rules/<int:rule_id>/rollback": ["propose_config_change"],      # the rollback_rule operation
    "api/schema/schemas": ["list_schemas"],
    "api/schema/tables": ["list_tables"],
    "api/schema/graph": ["describe_table"],                              # foreign keys are in each table's constraints
    "api/schema/export": ["export_schema"],
    "api/schema/tables/<str:name>": ["describe_table", "profile_column"],
    "api/schema/tables/<str:name>/export.csv": ["read_rows"],            # paged; the file download itself is a browser thing
    "api/schema/tables/<str:name>/rows": ["read_rows", "propose_change"],
    "api/schema/tables/<str:name>/options": ["lookup_options"],
    "api/query": ["query"],
    "api/duplicates": ["list_duplicates"],
    "api/duplicates/scan": ["find_duplicates"],
    "api/duplicates/<int:group_id>": ["get_duplicate"],
    "api/duplicates/<int:group_id>/dismiss": ["dismiss_duplicate"],
    "api/duplicates/<int:group_id>/reopen": ["reopen_duplicate"],
    "api/duplicates/<int:group_id>/merge": ["merge_duplicates"],
    "api/search": ["search_schema"],
    "api/search/reindex": ["reindex_search"],
    "api/audit": ["get_audit_log"],
    "api/scripts": ["submit_script", "get_script"],
    "api/scripts/limits": ["get_settings"],
    "api/scripts/<int:script_id>": ["get_script"],
    "api/scripts/<int:script_id>/validate": ["validate_script"],
    "api/scripts/<int:script_id>/submit": ["submit_script"],
    "api/users": ["list_users"],
    "api/users/<int:user_id>/role": ["propose_config_change"],
}

WEB_ONLY = {
    "api/health": "liveness probe for the gateway, not a database task",
    "api/csrf": "browser session: CSRF token",
    "api/me": "browser session: who is signed in",
    "api/auth/google/login": "sign-in is for a person, through Google",
    "api/auth/google/callback": "sign-in is for a person, through Google",
    "api/auth/logout": "browser session",
    "api/mcp-tokens": "an AI client must not mint or revoke its own access tokens",
    "api/mcp-tokens/<int:token_id>": "an AI client must not mint or revoke its own access tokens",
    "api/queue": "badge counts for the review tray; list_pending lists the items themselves",
    "api/chat/models": "web chat provider settings",
    "api/chat/conversations": "web chat conversations belong to the browser chat",
    "api/chat/conversations/<int:conv_id>": "web chat conversations belong to the browser chat",
    "api/chat/conversations/<int:conv_id>/messages": "web chat conversations belong to the browser chat",
    "api/scripts/uploads": "chunked upload of a file on a person's computer; an AI sends data with begin_load and append_rows",
    "api/scripts/uploads/<int:upload_id>": "chunked upload of a file on a person's computer",
    "api/scripts/uploads/<int:upload_id>/complete": "chunked upload of a file on a person's computer",
    "api/scripts/uploads/<int:upload_id>/<int:n>": "chunked upload of a file on a person's computer",
}


def api_routes() -> set[str]:
    """Every `api/` route in the URL configuration, as written, nested includes joined."""
    found: set[str] = set()

    def walk(patterns, prefix=""):
        for p in patterns:
            route = prefix + str(p.pattern)
            if isinstance(p, URLResolver):
                walk(p.url_patterns, route)
            elif isinstance(p, URLPattern) and route.startswith("api/"):
                found.add(route)

    walk(get_resolver().url_patterns)
    return found


def problems(routes, covered, web_only, known_tools) -> list[str]:
    out = []
    for r in sorted(routes):
        if r not in covered and r not in web_only:
            out.append(f"{r}: no MCP tool and no reason it is web-only (add it to COVERED or WEB_ONLY)")
        if r in covered and r in web_only:
            out.append(f"{r}: listed as both COVERED and WEB_ONLY")
    for r, names in covered.items():
        for n in names:
            if n not in known_tools:
                out.append(f"{r}: {n} is not an MCP tool")
        if not names:
            out.append(f"{r}: COVERED with no tools")
    for r, why in web_only.items():
        if not why.strip():
            out.append(f"{r}: WEB_ONLY needs a reason")
    for r in sorted((set(covered) | set(web_only)) - set(routes)):
        out.append(f"{r}: not a route any more (remove it)")
    return out


def test_every_api_route_is_covered_or_web_only():
    assert problems(api_routes(), COVERED, WEB_ONLY, set(tools.TOOL_ROLES)) == []


def test_the_route_walk_sees_nested_includes():
    routes = api_routes()
    assert "api/duplicates/<int:group_id>/reopen" in routes and "api/mcp-tokens" in routes     # the second is an include
    assert len(routes) > 40


def test_a_new_route_without_an_entry_fails():
    found = problems(api_routes() | {"api/brand-new"}, COVERED, WEB_ONLY, set(tools.TOOL_ROLES))
    assert found == ["api/brand-new: no MCP tool and no reason it is web-only (add it to COVERED or WEB_ONLY)"]


def test_a_missing_tool_and_a_double_entry_fail():
    bad = {**COVERED, "api/health": ["no_such_tool"]}
    found = problems(api_routes(), bad, WEB_ONLY, set(tools.TOOL_ROLES))
    assert any("no_such_tool is not an MCP tool" in f for f in found)
    assert any("both COVERED and WEB_ONLY" in f for f in found)
