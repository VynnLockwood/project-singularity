import asyncio
import json

import pytest
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import Client

from accounts.models import Role, User
from approvals.models import Change
from chat import openrouter, orchestrator
from chat.consumers import EventsConsumer
from chat.models import Conversation, Message
from chat.openrouter import ChatError
from config.siconfig import get_config

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]

MODEL = get_config()["chat"]["defaultModel"]
CREATE = [{"op": "create_table", "name": "customer", "columns": [{"name": "name", "type": "text"}]}]


def login(role):
    u = User.objects.create(username=f"{role}@example.com", email=f"{role}@example.com", role=role)
    c = Client()
    c.force_login(u)
    return c, u


def post(c, url, data=None):
    return c.post(url, data=json.dumps(data or {}), content_type="application/json")


def script(monkeypatch, *replies):
    """Make the model return these replies in order; records what it was sent."""
    seen = []
    queue = list(replies)

    def fake(messages, tools, model, **kw):
        seen.append({"messages": messages, "tools": [t["function"]["name"] for t in tools], "model": model})
        return queue.pop(0)

    monkeypatch.setattr(openrouter, "complete", fake)
    return seen


def say(text):
    return {"content": text, "tool_calls": []}


def call(name, args, id="c1"):
    return {"content": "", "tool_calls": [{"id": id, "name": name, "arguments": json.dumps(args)}]}


def new_conv(c):
    return post(c, "/api/chat/conversations").json()["id"]


def test_models_and_conversation_basics():
    c, _ = login(Role.VIEWER)
    assert c.get("/api/chat/models").json()["default"] == MODEL
    assert post(c, "/api/chat/conversations", {"model": "evil/model"}).status_code == 400
    cid = new_conv(c)
    assert [x["id"] for x in c.get("/api/chat/conversations").json()["conversations"]] == [cid]


def test_requires_sign_in():
    assert Client().get("/api/chat/conversations").status_code == 401


def test_plain_answer_is_stored_and_title_set(monkeypatch):
    c, _ = login(Role.VIEWER)
    seen = script(monkeypatch, say("hello"))
    cid = new_conv(c)
    r = post(c, f"/api/chat/conversations/{cid}/messages", {"content": "hi there"}).json()
    assert [m["role"] for m in r["messages"]] == ["user", "assistant"]
    detail = c.get(f"/api/chat/conversations/{cid}").json()
    assert detail["title"] == "hi there" and len(detail["messages"]) == 2
    assert seen[0]["messages"][0]["role"] == "system" and seen[0]["model"] == MODEL


def test_tools_offered_depend_on_role(monkeypatch):
    from mcp_server import tools as mcp_tools

    def offered(role):  # every tool the role may use, except deciding: that is the person's job in the web app
        return {t for t, need in mcp_tools.TOOL_ROLES.items()
                if mcp_tools.RANK[need] <= mcp_tools.RANK[role]} - {"approve_change", "reject_change"}

    assert "propose_change" not in offered("viewer") and "propose_change" in offered("editor")
    assert "list_users" not in offered("editor") and "list_users" in offered("approver")
    for role, expected in [(Role.VIEWER, offered("viewer")), (Role.EDITOR, offered("editor")), (Role.APPROVER, offered("approver"))]:
        User.objects.all().delete()
        c, _ = login(role)
        seen = script(monkeypatch, say("ok"))
        post(c, f"/api/chat/conversations/{new_conv(c)}/messages", {"content": "x"})
        assert set(seen[0]["tools"]) == expected, role
        assert "approve_change" not in seen[0]["tools"]  # approval is the popup, not a tool


def test_model_proposes_a_change_and_it_is_linked_to_the_conversation(monkeypatch):
    c, u = login(Role.EDITOR)
    seen = script(monkeypatch, call("propose_change", {"operations": CREATE, "prompt": "add customers"}),
                  say("Proposed; waiting for approval."))
    cid = new_conv(c)
    r = post(c, f"/api/chat/conversations/{cid}/messages", {"content": "add customers"}).json()
    assert [m["role"] for m in r["messages"]] == ["user", "assistant", "tool", "assistant"]
    ch = Change.objects.get()
    assert (ch.source, ch.conversation_id, ch.status, ch.requester_id) == ("web", cid, "pending", u.pk)
    assert [x["id"] for x in r["changes"]] == [ch.pk]
    tool_result = json.loads(r["messages"][2]["content"])
    assert "popup" in tool_result["next"] and "approve_change" not in tool_result["next"]
    # the tool result reached the model on the next round
    assert seen[1]["messages"][-1]["role"] == "tool"


def test_gate_feedback_goes_back_to_the_model(monkeypatch):
    c, _ = login(Role.EDITOR)
    bad = [{"op": "create_table", "name": "Bad_Name", "columns": [{"name": "x", "type": "text"}]}]
    seen = script(monkeypatch, call("propose_change", {"operations": bad}), say("fixing"))
    post(c, f"/api/chat/conversations/{new_conv(c)}/messages", {"content": "x"})
    fed_back = json.loads(seen[1]["messages"][-1]["content"])
    assert fed_back["status"] == "rejected" and fed_back["check"]["violations"][0]["suggestion"] == "bad_name"


@pytest.mark.parametrize("name,args", [
    ("propose_change", {"operations": CREATE}),   # viewer: not offered
    ("approve_change", {"change_id": 1}),         # nobody: not a chat tool
    ("nope", {}),
])
def test_unavailable_tool_calls_are_refused(monkeypatch, name, args):
    c, _ = login(Role.VIEWER)
    seen = script(monkeypatch, call(name, args), say("ok"))
    post(c, f"/api/chat/conversations/{new_conv(c)}/messages", {"content": "x"})
    assert "not available" in json.loads(seen[1]["messages"][-1]["content"])["error"]
    assert not Change.objects.exists()


@pytest.mark.parametrize("raw", ["not json", "[1,2]", json.dumps({"bogus": 1})])
def test_bad_tool_arguments_do_not_crash(monkeypatch, raw):
    c, _ = login(Role.EDITOR)
    reply = {"content": "", "tool_calls": [{"id": "c1", "name": "describe_table", "arguments": raw}]}
    seen = script(monkeypatch, reply, say("ok"))
    r = post(c, f"/api/chat/conversations/{new_conv(c)}/messages", {"content": "x"})
    assert r.status_code == 200
    assert "error" in json.loads(seen[1]["messages"][-1]["content"])


def test_tool_loop_is_capped(monkeypatch):
    c, _ = login(Role.VIEWER)
    rounds = get_config()["chat"]["maxToolRounds"]
    script(monkeypatch, *[call("get_rules", {}, id=f"c{i}") for i in range(rounds)])
    r = post(c, f"/api/chat/conversations/{new_conv(c)}/messages", {"content": "x"}).json()
    assert "too many steps" in r["messages"][-1]["content"]


def test_other_users_conversation_is_invisible(monkeypatch):
    a, _ = login(Role.VIEWER)
    cid = new_conv(a)
    User.objects.all().delete()
    b, _ = login(Role.APPROVER)
    assert b.get(f"/api/chat/conversations/{cid}").status_code == 404
    assert post(b, f"/api/chat/conversations/{cid}/messages", {"content": "x"}).status_code == 404


def test_rate_limit(monkeypatch):
    c, _ = login(Role.VIEWER)
    script(monkeypatch, *[say("ok")] * 100)
    cid = new_conv(c)
    limit = get_config()["chat"]["messagesPerMinute"]
    codes = [post(c, f"/api/chat/conversations/{cid}/messages", {"content": "x"}).status_code for _ in range(limit + 1)]
    assert codes[:limit] == [200] * limit and codes[-1] == 429


def test_empty_message_rejected():
    c, _ = login(Role.VIEWER)
    assert post(c, f"/api/chat/conversations/{new_conv(c)}/messages", {"content": "  "}).status_code == 400


def test_provider_failure_is_reported_not_raised(monkeypatch):
    c, _ = login(Role.VIEWER)

    def boom(*a, **k):
        raise ChatError("unavailable", "OpenRouter could not be reached")

    monkeypatch.setattr(openrouter, "complete", boom)
    r = post(c, f"/api/chat/conversations/{new_conv(c)}/messages", {"content": "x"})
    assert r.status_code == 502 and r.json()["code"] == "unavailable"


# ---- the OpenRouter client ---------------------------------------------------

class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


def test_client_parses_tool_calls(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    sent = {}

    def fake_post(url, json, headers, timeout):
        sent.update(url=url, body=json, auth=headers["Authorization"])
        return FakeResp(200, {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "a", "function": {"name": "get_rules", "arguments": "{}"}}]}}]})

    monkeypatch.setattr(openrouter.requests, "post", fake_post)
    out = openrouter.complete([{"role": "user", "content": "x"}], [{"type": "function"}], "m/x")
    assert out == {"content": "", "tool_calls": [{"id": "a", "name": "get_rules", "arguments": "{}"}]}
    assert sent["auth"] == "Bearer k" and sent["body"]["tool_choice"] == "auto" and sent["url"].endswith("/chat/completions")


@pytest.mark.parametrize("key,resp,code", [
    ("", FakeResp(), "not_configured"),
    ("k", FakeResp(500), "provider_error"),
    ("k", FakeResp(200, {"weird": 1}), "provider_error"),
])
def test_client_errors(monkeypatch, key, resp, code):
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.setattr(openrouter.requests, "post", lambda *a, **k: resp)
    with pytest.raises(ChatError) as e:
        openrouter.complete([], [], "m/x")
    assert e.value.code == code


def test_client_network_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    def down(*a, **k):
        raise openrouter.requests.ConnectionError("x")

    monkeypatch.setattr(openrouter.requests, "post", down)
    with pytest.raises(ChatError) as e:
        openrouter.complete([], [], "m/x")
    assert e.value.code == "unavailable"


# ---- WebSocket events --------------------------------------------------------

def communicator(user):
    comm = WebsocketCommunicator(EventsConsumer.as_asgi(), "/ws/events")
    comm.scope["user"] = user
    return comm


@pytest.mark.django_db(transaction=True)
def test_anonymous_websocket_is_refused():
    from django.contrib.auth.models import AnonymousUser

    async def go():
        ok, code = await communicator(AnonymousUser()).connect()
        return ok, code

    ok, code = asyncio.run(go())
    assert not ok and code == 4401


@pytest.mark.django_db(transaction=True)
def test_approver_and_requester_get_change_events_but_other_editors_do_not():
    from approvals import service

    editor = User.objects.create(username="e@x.com", email="e@x.com", role=Role.EDITOR)
    other = User.objects.create(username="o@x.com", email="o@x.com", role=Role.EDITOR)
    approver = User.objects.create(username="a@x.com", email="a@x.com", role=Role.APPROVER)

    async def go():
        comms = {n: communicator(u) for n, u in [("editor", editor), ("other", other), ("approver", approver)]}
        for c in comms.values():
            assert (await c.connect())[0]
        await sync_to_async(service.propose)(editor, CREATE, source="web")
        got = {}
        for n, c in comms.items():
            if await c.receive_nothing(timeout=0.3) is False:
                got[n] = await c.receive_json_from()
            await c.disconnect()
        return got

    got = asyncio.run(go())
    assert set(got) == {"editor", "approver"}
    assert got["approver"]["type"] == "change" and got["approver"]["change"]["status"] == "pending"
    assert got["approver"]["change"]["diff"][0].startswith("+ create table customer")
