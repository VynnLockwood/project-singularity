import json

import pytest
from django.test import Client

from accounts.models import Role, User
from chat import errors, opencode, openrouter, orchestrator, providers
from chat.models import Conversation
from config.siconfig import get_config
from settings_app import tunables


@pytest.fixture(autouse=True)
def _fresh_config():
    get_config.cache_clear()
    yield
    get_config.cache_clear()


def test_one_chat_error_class():
    assert openrouter.ChatError is opencode.ChatError is errors.ChatError


def test_dispatch_routes_by_provider(monkeypatch):
    calls = []
    monkeypatch.setitem(providers._CLIENTS, "opencode", lambda m, t, model, **kw: calls.append(("opencode", model)) or {"content": "a", "tool_calls": []})
    monkeypatch.setitem(providers._CLIENTS, "openrouter", lambda m, t, model, **kw: calls.append(("openrouter", model)) or {"content": "b", "tool_calls": []})
    assert providers.complete("opencode", [], [], "m1")["content"] == "a"
    assert calls == [("opencode", "m1")]
    assert providers.complete("openrouter", [], [], "m2")["content"] == "b"
    assert calls == [("opencode", "m1"), ("openrouter", "m2")]


def test_unknown_provider_is_not_configured():
    with pytest.raises(errors.ChatError) as exc:
        providers.complete("nope", [], [], "m")
    assert exc.value.code == "not_configured"


@pytest.mark.django_db
@pytest.mark.usefixtures("master")
def test_run_turn_uses_configured_provider(monkeypatch):
    approver = User.objects.create(username="ap@example.com", email="ap@example.com", role=Role.APPROVER)
    tunables.apply(approver, {"chat.provider": "opencode"})
    seen = []
    monkeypatch.setitem(providers._CLIENTS, "opencode", lambda m, t, model, **kw: seen.append(model) or {"content": "ok", "tool_calls": []})
    conv = Conversation.objects.create(user=approver, model="m1")
    added = orchestrator.run_turn(conv, approver, "hi")
    assert seen and added[-1].content == "ok"


@pytest.mark.django_db
@pytest.mark.usefixtures("master")
def test_opencode_error_is_handled_by_the_view(monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    approver = User.objects.create(username="ap@example.com", email="ap@example.com", role=Role.APPROVER)
    tunables.apply(approver, {"chat.provider": "opencode"})
    c = Client()
    c.force_login(approver)
    cid = c.post("/api/chat/conversations", data="{}", content_type="application/json").json()["id"]
    r = c.post(f"/api/chat/conversations/{cid}/messages", data=json.dumps({"content": "hi"}), content_type="application/json")
    assert r.status_code == 503
    assert r.json()["code"] == "not_configured"
