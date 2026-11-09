import json

import pytest

from accounts.models import Role, User
from audit.models import AuditLog
from config import siconfig
from config.siconfig import get_config
from settings_app import tunables
from settings_app.models import Setting

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_config():
    get_config.cache_clear()
    yield
    get_config.cache_clear()


def user(role):
    return User.objects.create(username=f"{role}@x.com", email=f"{role}@x.com", role=role)


def client_for(u):
    from django.test import Client
    c = Client()
    c.force_login(u)
    return c


def test_seed_comes_from_yaml_until_overridden(monkeypatch):
    get_config.cache_clear()
    monkeypatch.setitem(siconfig._yaml_config()["chat"], "allowedModels", ["a/b"])
    monkeypatch.setitem(siconfig._yaml_config()["chat"], "defaultModel", "a/b")  # the seed also includes the default
    models = get_config()["chat"]["models"]
    assert models == ["a/b"]
    models.append("mutated")  # a copy: the file's list is never exposed
    assert siconfig._yaml_config()["chat"]["allowedModels"] == ["a/b"]


def test_add_models_and_default_in_one_request():
    a = user(Role.APPROVER)
    tunables.apply(a, {"chat.models": ["x/y", "z/w"], "chat.defaultModel": "z/w"})
    cfg = get_config()["chat"]
    assert cfg["models"] == ["x/y", "z/w"] and cfg["defaultModel"] == "z/w"
    assert AuditLog.objects.filter(action="settings.updated").exists()
    row = next(r for r in tunables.describe() if r["key"] == "chat.models")
    assert row["kind"] == "models" and row["value"] == ["x/y", "z/w"] and row["overridden"] is True
    assert isinstance(row["default"], list)


@pytest.mark.parametrize("bad", ["", "has space", "a'b", "a\nb", "x" * 121, 5, None])
def test_reject_bad_ids(bad):
    a = user(Role.APPROVER)
    with pytest.raises(tunables.TunableError) as e:
        tunables.apply(a, {"chat.models": ["ok/m", bad], "chat.defaultModel": "ok/m"})
    assert "model" in str(e.value).lower()
    assert not Setting.objects.exists()


def test_reject_duplicates_too_many_and_non_list():
    a = user(Role.APPROVER)
    for bad in (["a/b", "a/b"], [f"m/{i}" for i in range(51)], "a/b", []):
        with pytest.raises(tunables.TunableError):
            tunables.apply(a, {"chat.models": bad, "chat.defaultModel": "a/b"})
    assert not Setting.objects.exists()
    tunables.apply(a, {"chat.models": [f"m/{i}" for i in range(50)], "chat.defaultModel": "m/0"})


def test_default_must_be_in_list():
    a = user(Role.APPROVER)
    with pytest.raises(tunables.TunableError):
        tunables.apply(a, {"chat.defaultModel": "unknown/m"})
    with pytest.raises(tunables.TunableError) as e:
        tunables.apply(a, {"chat.models": ["q/r"]})
    assert "default model must stay in the list" in str(e.value)
    assert not Setting.objects.exists()


def test_reset_models_with_dangling_default_rejected():
    a = user(Role.APPROVER)
    tunables.apply(a, {"chat.models": ["a/1", "a/2"], "chat.defaultModel": "a/2"})
    with pytest.raises(tunables.TunableError) as e:
        tunables.apply(a, {"chat.models": None})
    assert "default model must stay in the list" in str(e.value)
    assert get_config()["chat"]["models"] == ["a/1", "a/2"]


def test_editor_cannot_change():
    with pytest.raises(tunables.TunableError) as e:
        tunables.apply(user(Role.EDITOR), {"chat.models": ["x/y"]})
    assert "only approvers" in str(e.value)


def test_chat_models_endpoint_and_create_conversation():
    a = user(Role.APPROVER)
    tunables.apply(a, {"chat.models": ["x/y", "anthropic/claude-sonnet-4.5"]})
    c = client_for(a)
    body = json.loads(c.get("/api/chat/models").content)
    assert "x/y" in body["allowed"] and body["default"]
    r = c.post("/api/chat/conversations", data=json.dumps({"model": "x/y"}), content_type="application/json")
    assert r.status_code == 201
    r = c.post("/api/chat/conversations", data=json.dumps({"model": "nope/m"}), content_type="application/json")
    assert r.status_code == 400


@pytest.mark.usefixtures("master")
def test_site_change_reaches_the_next_chat(monkeypatch):
    from chat import orchestrator, providers
    from chat.models import Conversation

    a = user(Role.APPROVER)
    c = client_for(a)
    r = c.patch(
        "/api/settings",
        data=json.dumps({"chat.provider": "opencode", "chat.models": ["x/y"], "chat.defaultModel": "x/y"}),
        content_type="application/json",
    )
    assert r.status_code == 200, r.content
    get_config.cache_clear()

    r = c.post("/api/chat/conversations", data="{}", content_type="application/json")
    assert r.status_code == 201
    conv = Conversation.objects.get(pk=r.json()["id"])
    assert conv.model == "x/y"

    seen = []
    monkeypatch.setitem(
        providers._CLIENTS, "opencode",
        lambda m, t, model, **kw: seen.append(model) or {"content": "ok", "tool_calls": []},
    )
    added = orchestrator.run_turn(conv, a, "hi")
    assert seen == ["x/y"] and added[-1].content == "ok"


def _yaml(monkeypatch, tmp_path, text):
    p = tmp_path / "singularity.yaml"
    p.write_text(text)
    monkeypatch.setenv("SINGULARITY_CONFIG", str(p))
    get_config.cache_clear()


def test_yaml_default_outside_allowed_models_still_works(monkeypatch, tmp_path):
    _yaml(monkeypatch, tmp_path, "chat:\n  defaultModel: d/m\n")
    assert get_config()["chat"]["models"] == ["d/m"]
    c = client_for(user(Role.VIEWER))
    body = json.loads(c.get("/api/chat/models").content)
    assert body["default"] == "d/m" and "d/m" in body["allowed"]
    r = c.post("/api/chat/conversations", data="{}", content_type="application/json")
    assert r.status_code == 201
    r = c.post("/api/chat/conversations", data=json.dumps({"model": "d/m"}), content_type="application/json")
    assert r.status_code == 201


def test_seed_appends_default_once_and_keeps_order(monkeypatch, tmp_path):
    _yaml(monkeypatch, tmp_path, "chat:\n  defaultModel: a/2\n  allowedModels: [a/1, a/2]\n")
    assert get_config()["chat"]["models"] == ["a/1", "a/2"]
    _yaml(monkeypatch, tmp_path, "chat:\n  defaultModel: d/m\n  allowedModels: [a/1, a/2]\n")
    assert get_config()["chat"]["models"] == ["a/1", "a/2", "d/m"]


def test_full_reset_with_yaml_default_outside_allowed(monkeypatch, tmp_path):
    _yaml(monkeypatch, tmp_path, "chat:\n  defaultModel: d/m\n  allowedModels: [a/1]\n")
    a = user(Role.APPROVER)
    tunables.apply(a, {"chat.models": ["x/y"], "chat.defaultModel": "x/y"})
    tunables.apply(a, {"chat.models": None, "chat.defaultModel": None})
    cfg = get_config()["chat"]
    assert cfg["models"] == ["a/1", "d/m"] and cfg["defaultModel"] == "d/m"
    assert not Setting.objects.filter(key__startswith="chat.").exists()


def test_override_lists_are_copies(monkeypatch):
    a = user(Role.APPROVER)
    tunables.apply(a, {"chat.models": ["x/y", "z/w"], "chat.defaultModel": "x/y"})
    get_config()["chat"]["models"].append("mutated")
    assert get_config()["chat"]["models"] == ["x/y", "z/w"]
