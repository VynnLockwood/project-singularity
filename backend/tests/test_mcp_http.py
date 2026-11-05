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

    assert json.load(urllib.request.urlopen(server["base"] + "/healthz")) == {"ok": True}


def test_each_role_sees_only_its_tools(server):
    read = ["describe_table", "get_rules", "list_tables", "query"]
    edit = read + ["approve_change", "get_change", "propose_change", "reject_change"]
    assert names(server, Role.VIEWER) == read
    assert names(server, Role.EDITOR) == sorted(edit)
    assert names(server, Role.APPROVER) == sorted(edit + ["list_pending"])


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
    assert names(server, Role.APPROVER) == ["describe_table", "get_rules", "list_tables", "query"]


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
        {"name": "customerCode", "type": "text", "unique": True}, {"name": "name", "type": "text"}]}]

    # 1. the editor's AI proposes a schema change; it waits for an approver
    ch = payload(with_client(url, tk[Role.EDITOR], lambda c: c.call_tool(
        "propose_change", {"operations": create, "prompt": "add customers"})))
    assert ch["status"] == "pending" and ch["kind"] == "schema" and ch["source"] == "mcp"
    assert "needs an approver" in ch["next"] and ch["diff"][0].startswith("+ create table customer")

    # 2. the editor's AI cannot approve it, even if it tries
    res = with_client(url, tk[Role.EDITOR], lambda c: c.call_tool("approve_change", {"change_id": ch["id"]}))
    assert res.is_error and "cannot approve" in res.content[0].text

    # 3. the approver's AI sees it in the queue and, after the user confirms, approves
    pending = payload(with_client(url, tk[Role.APPROVER], lambda c: c.call_tool("list_pending", {})))
    assert [p["id"] for p in pending["pending"]] == [ch["id"]]
    done = payload(with_client(url, tk[Role.APPROVER], lambda c: c.call_tool("approve_change", {"change_id": ch["id"]})))
    assert done["status"] == "applied"

    # 4. everybody can read it back
    tables = payload(with_client(url, tk[Role.VIEWER], lambda c: c.call_tool("list_tables", {})))
    assert [t["name"] for t in tables["tables"]] == ["customer"]
    d = payload(with_client(url, tk[Role.VIEWER], lambda c: c.call_tool("describe_table", {"table": "customer"})))
    assert d["columns"][0]["name"] == "id"

    # 5. an editor inserts data and approves their own data change
    ins = payload(with_client(url, tk[Role.EDITOR], lambda c: c.call_tool("propose_change", {"operations": [
        {"op": "insert_rows", "table": "customer", "rows": [{"customerCode": "C1", "name": "Acme"}]}]})))
    assert "Only after they say yes" in ins["next"]
    assert payload(with_client(url, tk[Role.EDITOR], lambda c: c.call_tool(
        "approve_change", {"change_id": ins["id"]})))["status"] == "applied"
    q = payload(with_client(url, tk[Role.VIEWER], lambda c: c.call_tool(
        "query", {"sql": 'SELECT "customerCode", "createdBy" FROM customer'})))
    assert q["rows"] == [["C1", "editor@example.com"]]


def test_gate_feedback_reaches_the_ai(server):
    bad = [{"op": "create_table", "name": "Supplier_Contact", "columns": [{"name": "x", "type": "text"}]}]
    ch = payload(with_client(server["url"], server["tokens"][Role.EDITOR],
                             lambda c: c.call_tool("propose_change", {"operations": bad})))
    assert ch["status"] == "rejected"
    assert ch["check"]["violations"][0]["suggestion"] == "supplierContact"
    assert "Fix the problems" in ch["next"]


def test_query_tool_cannot_write(server):
    res = with_client(server["url"], server["tokens"][Role.APPROVER],
                      lambda c: c.call_tool("query", {"sql": "DROP TABLE customer"}))
    assert res.is_error and "only SELECT" in res.content[0].text
