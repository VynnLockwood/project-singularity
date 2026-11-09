import os
import pytest
from django.test import RequestFactory

from accounts.models import Role, User
from chat import openrouter, opencode, providers


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)


def test_browser_key_is_used_instead_of_env(monkeypatch):
    """Per-request key reaches the Authorization header; env stays unused."""
    captured = {}
    class R:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "hi", "tool_calls": []}}]}
    monkeypatch.setattr(openrouter.requests, "post",
                        lambda *a, **kw: captured.update(kw.get("headers") or {}) or R())
    openrouter.complete([{"role": "user", "content": "x"}], [], "m", key="sk-browser")
    assert captured["Authorization"] == "Bearer sk-browser"


def test_env_used_when_no_key_supplied(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-server")
    captured = {}
    class R:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "hi", "tool_calls": []}}]}
    monkeypatch.setattr(openrouter.requests, "post",
                        lambda *a, **kw: captured.update(kw.get("headers") or {}) or R())
    openrouter.complete([{"role": "user", "content": "x"}], [], "m")
    assert captured["Authorization"] == "Bearer sk-server"


def test_browser_key_wins_over_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-server")
    captured = {}
    class R:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "hi", "tool_calls": []}}]}
    monkeypatch.setattr(openrouter.requests, "post",
                        lambda *a, **kw: captured.update(kw.get("headers") or {}) or R())
    openrouter.complete([{"role": "user", "content": "x"}], [], "m", key="sk-browser")
    assert captured["Authorization"] == "Bearer sk-browser"
