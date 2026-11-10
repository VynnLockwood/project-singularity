"""End to end: a real uvicorn server and the MCP SDK's own client, over HTTP, with bearer tokens."""
import asyncio
import json
import os
import socket
import threading
import time

os.environ.setdefault("MCP_ALLOWED_HOSTS", "127.0.0.1,localhost")

import httpx2  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from accounts import tokens  # noqa: E402
from accounts.models import Role, User  # noqa: E402

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]


@pytest.fixture
def server(master):
    users = {r: User.objects.create(username=f"{r}@example.com", email=f"{r}@example.com", role=r)
             for r in (Role.VIEWER, Role.EDITOR, Role.APPROVER)}
    raw = {r: tokens.create_token(u, "e2e")[1] for r, u in users.items()}

    from mcp_server.app import build_app

    app = build_app()

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started, "server did not start"
    yield {"url": f"http://127.0.0.1:{port}/mcp", "base": f"http://127.0.0.1:{port}", "tokens": raw, "users": users}
    srv.should_exit = True
    thread.join(10)


def with_client(url, token, fn):
    async def go():
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
            async with Client(streamable_http_client(url, http_client=http)) as c:
                return await fn(c)

    return asyncio.run(go())


def payload(result):
    """The tool's JSON result."""
    if getattr(result, "structured_content", None):
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


def names(server, role):
    return sorted(t.name for t in with_client(server["url"], server["tokens"][role], lambda c: c.list_tools()).tools)


def test_missing_or_bad_token_is_401(server):
    import urllib.request
    import urllib.error

    for headers in ({}, {"Authorization": "Bearer sgl_nope"}, {"Authorization": "Basic abc"}):
        req = urllib.request.Request(server["url"], data=b"{}", headers={"Content-Type": "application/json", **headers})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 401
        assert "Bearer" in e.value.headers["WWW-Authenticate"]


def test_health_endpoint_needs_no_token(server):
    import urllib.request

    assert json.load(urllib.request.urlopen(server["base"] + "/healthz"))["ok"] is True


def tools_up_to(role):
    """Every tool whose minimum role is at or below `role`, as the server should list them."""
    from mcp_server import tools
    return sorted(t for t, need in tools.TOOL_ROLES.items() if tools.RANK[need] <= tools.RANK[role])


def test_each_role_sees_only_its_tools(server):
    viewer, editor, approver = (names(server, r) for r in (Role.VIEWER, Role.EDITOR, Role.APPROVER))
    assert viewer == tools_up_to("viewer") and editor == tools_up_to("editor") and approver == tools_up_to("approver")
    # the lines that matter, spelled out: reading for all, proposing from editor up, deciding for approvers only
    assert {"query", "list_tables", "get_rules"} <= set(viewer)
    assert "propose_change" not in viewer and "propose_change" in editor
    assert not {"approve_change", "reject_change", "list_pending", "list_users"} & set(editor)
    assert {"approve_change", "reject_change", "list_pending", "list_users"} <= set(approver)
    assert set(viewer) < set(editor) < set(approver)


def test_viewer_cannot_call_a_hidden_tool(server):
    async def go(c):
        return await c.call_tool("propose_change", {"operations": []})

    res = with_client(server["url"], server["tokens"][Role.VIEWER], go)
    assert res.is_error and "not available for your role" in res.content[0].text


def test_role_change_applies_immediately_to_the_same_token(server):
    u = server["users"][Role.APPROVER]
    assert "list_pending" in names(server, Role.APPROVER)
    u.role = Role.VIEWER
    u.save()
    assert names(server, Role.APPROVER) == tools_up_to("viewer")
    assert "list_pending" not in names(server, Role.APPROVER)


def test_revoked_token_stops_working(server):
    import urllib.error

    tokens.revoke(server["users"][Role.EDITOR], server["users"][Role.EDITOR].mcp_tokens.first().pk)
    with pytest.raises(Exception):
        names(server, Role.EDITOR)


def test_full_flow_ai_proposes_user_confirms_then_applies(server):
    url, tk = server["url"], server["tokens"]

    rules = payload(with_client(url, tk[Role.EDITOR], lambda c: c.call_tool("get_rules", {})))
    assert any(r["name"] == "tableNaming" for r in rules["rules"])

    create = [{"op": "create_table", "name": "customer", "columns": [
        {"name": "customer_code", "type": "text", "unique": True}, {"name": "name", "type": "text"}]}]

    # 1. the editor's AI proposes a schema change; it waits for an approver
    ch = payload(with_client(url, tk[Role.EDITOR], lambda c: c.call_tool(
        "propose_change", {"operations": create, "prompt": "add customers"})))
    assert ch["status"] == "pending" and ch["kind"] == "schema" and ch["source"] == "mcp"
    assert "needs an approver" in ch["next"] and ch["diff"][0].startswith("+ create table customer")

    # 2. the editor's AI cannot even reach the decision tools, let alone approve
    res = with_client(url, tk[Role.EDITOR], lambda c: c.call_tool("approve_change", {"change_id": ch["id"]}))
    assert res.is_error and "not available for your role" in res.content[0].text

    # 3. the approver's AI sees it in the queue and, after the user confirms, approves
    pending = payload(with_client(url, tk[Role.APPROVER], lambda c: c.call_tool("list_pending", {})))
    assert [p["id"] for p in pending["pending"]] == [ch["id"]]
    done = payload(with_client(url, tk[Role.APPROVER], lambda c: c.call_tool("approve_change", {"change_id": ch["id"]})))
    assert done["status"] == "applied"

    # 4. everybody can read it back
    tables = payload(with_client(url, tk[Role.VIEWER], lambda c: c.call_tool("list_tables", {})))
    assert [t["name"] for t in tables["tables"]] == ["customer"]
    d = payload(with_client(url, tk[Role.VIEWER], lambda c: c.call_tool("describe_table", {"table": "customer"})))
    assert d["columns"][0]["name"] == "customer_id"

    # 5. an editor inserts data; the change still asks its requester to confirm, but only the
    #    approver's client can apply it (self-approval is a web-app thing, see test_changes.py)
    ins = payload(with_client(url, tk[Role.EDITOR], lambda c: c.call_tool("propose_change", {"operations": [
        {"op": "insert_rows", "table": "customer", "rows": [{"customer_code": "C1", "name": "Acme"}]}]})))
    assert "needs an approver" in ins["next"]
    assert payload(with_client(url, tk[Role.APPROVER], lambda c: c.call_tool(
        "approve_change", {"change_id": ins["id"]})))["status"] == "applied"
    q = payload(with_client(url, tk[Role.VIEWER], lambda c: c.call_tool(
        "query", {"sql": 'SELECT customer_code, created_by FROM customer'})))
    assert q["rows"] == [["C1", "editor@example.com"]]


def test_gate_feedback_reaches_the_ai(server):
    bad = [{"op": "create_table", "name": "Supplier_Contact", "columns": [{"name": "x", "type": "text"}]}]
    ch = payload(with_client(server["url"], server["tokens"][Role.EDITOR],
                             lambda c: c.call_tool("propose_change", {"operations": bad})))
    assert ch["status"] == "rejected"
    assert ch["check"]["violations"][0]["suggestion"] == "supplier_contact"
    assert "Fix the problems" in ch["next"]


def test_query_tool_cannot_write(server):
    res = with_client(server["url"], server["tokens"][Role.APPROVER],
                      lambda c: c.call_tool("query", {"sql": "DROP TABLE customer"}))
    assert res.is_error and "only SELECT" in res.content[0].text


def test_connection_link_authenticates_without_a_header(server):
    """/mcp/t/<token> works for clients that cannot send an Authorization header."""
    async def go(url):
        async with httpx2.AsyncClient() as http:
            async with Client(streamable_http_client(url, http_client=http)) as c:
                return sorted(t.name for t in (await c.list_tools()).tools)

    link = f"{server['base']}/mcp/t/{server['tokens'][Role.VIEWER]}"
    assert asyncio.run(go(link)) == names(server, Role.VIEWER)


def test_connection_link_with_bad_token_is_401(server):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"{server['base']}/mcp/t/sgl_nope", data=b"{}", headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 401


def test_401_points_oauth_clients_at_the_discovery_document(server):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(server["url"], data=b"{}", headers={"Content-Type": "application/json", "X-Forwarded-Proto": "https"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 401
    assert 'resource_metadata="https://127.0.0.1:' in e.value.headers["WWW-Authenticate"]
    assert "/.well-known/oauth-protected-resource" in e.value.headers["WWW-Authenticate"]


def test_a_settings_change_proposed_over_mcp_waits_for_an_approver(server):
    """The whole path: an editor's AI client proposes a setting; it is pending, nothing changed; an approver's client applies it."""
    from config.siconfig import get_config

    tk = server["tokens"]
    before = get_config()["search"]["topK"]
    ops = [{"op": "set_setting", "key": "search.topK", "value": before + 2}]
    res = payload(with_client(server["url"], tk[Role.EDITOR], lambda c: c.call_tool("propose_config_change", {"operations": ops, "prompt": "more results"})))
    assert res["status"] == "pending" and res["kind"] == "config" and "approver" in res["next"]
    assert get_config()["search"]["topK"] == before

    done = payload(with_client(server["url"], tk[Role.APPROVER], lambda c: c.call_tool("approve_change", {"change_id": res["id"]})))
    assert done["status"] == "applied"
    get_config.cache_clear()
    assert get_config()["search"]["topK"] == before + 2


def test_new_tools_work_over_http(server):
    """The reach tools answer through a real MCP client, with structured results."""
    url, tk = server["url"], server["tokens"]
    made = payload(with_client(url, tk[Role.APPROVER], lambda c: c.call_tool("propose_change", {"operations": [
        {"op": "create_table", "name": "region", "columns": [{"name": "name_en", "type": "text"}]}]})))
    assert payload(with_client(url, tk[Role.APPROVER], lambda c: c.call_tool("approve_change", {"change_id": made["id"]})))["status"] == "applied"

    history = payload(with_client(url, tk[Role.VIEWER], lambda c: c.call_tool("list_changes", {"status": "applied"})))
    assert [c["id"] for c in history["changes"]] == [made["id"]] and history["truncated"] is False
    rows = payload(with_client(url, tk[Role.VIEWER], lambda c: c.call_tool("read_rows", {"table": "region", "limit": 5})))
    assert rows["total"] == 0 and rows["primaryKey"] == "region_id"
    dump = payload(with_client(url, tk[Role.VIEWER], lambda c: c.call_tool("export_schema", {})))
    assert "CREATE TABLE" in dump["sql"] and dump["truncated"] is False

    hidden = with_client(url, tk[Role.EDITOR], lambda c: c.call_tool("get_audit_log", {}))
    assert hidden.is_error and "not available for your role" in hidden.content[0].text
    log = payload(with_client(url, tk[Role.APPROVER], lambda c: c.call_tool("get_audit_log", {"limit": 5})))
    assert log["entries"] and set(log["entries"][0]) == {"id", "at", "actor", "action", "target", "details"}
    bad = with_client(url, tk[Role.VIEWER], lambda c: c.call_tool("read_rows", {"table": "nope"}))
    assert bad.is_error and "list_tables" in bad.content[0].text
    undone = payload(with_client(url, tk[Role.APPROVER], lambda c: c.call_tool("revert_change", {"change_id": made["id"]})))
    assert undone["status"] == "applied" and undone["source"] == "revert"
