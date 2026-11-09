"""Chat provider settings are approver-gated, and no API key value ever reaches a response.

Deliberately DB-free: everything else in tests/ needs a Postgres superuser (TEST_PG_ADMIN_USER), which
is not available here, so these exercise the pure helpers and the view's own gate/serialisation.
"""
import json
from types import SimpleNamespace

import pytest
from django.test import RequestFactory

from settings_app import tunables
from settings_app.views import settings as settings_view

CANARY = "sentinel-" + "9f3c1d"


def _request(method, *, approver=False, body=None):
    req = getattr(RequestFactory(), method.lower())(
        "/api/settings", data=json.dumps(body) if body else None, content_type="application/json"
    )
    req.user = SimpleNamespace(is_authenticated=True, can_read=True, can_approve=approver)
    return req


def test_get_reports_which_providers_have_a_key_without_returning_it(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", CANARY)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setattr(tunables, "describe", lambda: [])  # the other knobs need the DB; not under test here
    r = settings_view(_request("GET"))
    assert r.status_code == 200
    body = json.loads(r.content)
    assert body["keySet"] == {"openrouter": True, "opencode": False}
    assert all(isinstance(v, bool) for v in body["keySet"].values())
    assert CANARY not in r.content.decode()


def test_provider_must_be_one_of_the_known_providers():
    assert tunables._clean("chat.provider", "opencode") == "opencode"
    assert tunables._clean("chat.provider", "openrouter") == "openrouter"
    with pytest.raises(ValueError):
        tunables._clean("chat.provider", "evil-ai")


def test_only_approvers_can_change_chat_settings():
    body = {"chat.provider": "opencode"}
    assert settings_view(_request("PATCH", body=body)).status_code == 403


def test_an_approver_can_set_the_provider(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", CANARY)
    sent = {}
    monkeypatch.setattr(tunables, "apply", lambda actor, changes: sent.update(changes) or [])
    r = settings_view(_request("PATCH", approver=True, body={"chat.provider": "opencode"}))
    assert r.status_code == 200
    assert sent == {"chat.provider": "opencode"}
    body = json.loads(r.content)
    assert body["keySet"]["opencode"] is True
    assert CANARY not in r.content.decode()
