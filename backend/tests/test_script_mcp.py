"""Task 11: script tools over MCP (roles, inline cap, trimmed report, e2e submit)."""
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

MINI_PATH = os.path.join(os.path.dirname(__file__), "data", "mini_dump.sql")
with open(MINI_PATH, encoding="utf-8") as _f:
    MINI = _f.read()


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
    if getattr(result, "structured_content", None):
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


def call(server, token, tool, arguments):
    return with_client(server["url"], token, lambda c: c.call_tool(tool, arguments))


def names(server, token):
    return sorted(t.name for t in with_client(server["url"], token, lambda c: c.list_tools()).tools)


def test_tools_roles(server):
    n_view = names(server, server["tokens"][Role.VIEWER])
    assert "submit_script" not in n_view and "validate_script" not in n_view and "get_script" not in n_view
    n_edit = names(server, server["tokens"][Role.EDITOR])
    assert {"submit_script", "validate_script", "get_script"} <= set(n_edit)


def test_inline_over_cap_rejected(server):
    from config.siconfig import get_config
    big = "-- " + "x" * (get_config()["limits"]["maxInlineScriptBytes"] + 10)
    res = call(server, server["tokens"][Role.EDITOR], "submit_script",
               {"name": "big.sql", "content": big})
    assert res.is_error and "inline cap" in res.content[0].text


def test_validate_then_submit_e2e(server, approver_not_used=None):
    tk = server["tokens"]
    res = payload(call(server, tk[Role.EDITOR], "submit_script", {"name": "mini.sql", "content": MINI}))
    assert res["report"]["ok"], res["report"]["errors"]
    assert 0 < res["change_id"]
    # report is trimmed
    for t in res["report"].get("tables", []):
        assert len(t.get("sample_orphans", [])) <= 10
    assert len(res["report"].get("errors", [])) <= 20
    change_id = res["change_id"]
    done = payload(call(server, tk[Role.APPROVER], "approve_change", {"change_id": change_id}))
    assert done["status"] == "applied"
    # script row now points at the change
    got = payload(call(server, tk[Role.APPROVER], "get_script", {"script_id": res["script_id"]}))
    assert got["status"] == "submitted" and got["change_id"] == change_id


def test_validate_script_refuses_submitted(server):
    # spec: a submitted script cannot be re-validated (its file is locked while the change is decided)
    tk = server["tokens"][Role.EDITOR]
    res = payload(call(server, tk, "submit_script", {"name": "m.sql", "content": MINI}))
    res2 = payload(call(server, tk, "validate_script", {"script_id": res["script_id"]}))
    errs = res2["report"]["errors"]
    assert res2["report"]["ok"] is False and any("submitted" in e["message"] for e in errs)


def test_validate_script_before_submit(server):
    tk = server["tokens"][Role.EDITOR]
    up = payload(call(server, tk, "submit_script", {"name": "m.sql", "content": MINI, "validate_only": True})) if False else None
    # upload without submit: use validate_script on a script uploaded through the REST path? Keep MCP-only: submit_script
    # auto-submits when ok. So pre-submit validation happens inside submit_script; re-validate a failing script:
    bad = MINI + "COPY province (province_id) FROM stdin;\n99\t\n\\.\n"  # wrong column count → rows error
    res = call(server, tk, "submit_script", {"name": "bad.sql", "content": bad})
    assert not res.is_error
    body = payload(res)
    assert body.get("report", {}).get("ok") is False and not body.get("change_id")
    again = payload(call(server, tk, "validate_script", {"script_id": body["script_id"]}))
    assert again["report"]["ok"] is False and again["report"]["errors"] == body["report"]["errors"]
