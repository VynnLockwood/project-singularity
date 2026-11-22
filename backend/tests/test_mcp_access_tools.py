"""Admins manage per-database grants over MCP (get_user_access, set_user_access), and /api/me shows a person's
own databases with the level on each."""
import json
import os
import socket
import threading
import time

os.environ.setdefault("MCP_ALLOWED_HOSTS", "127.0.0.1,localhost")

import pytest  # noqa: E402
import uvicorn  # noqa: E402

from accounts import services, tokens  # noqa: E402
from accounts.models import Role, SchemaGrant, User  # noqa: E402
from audit.models import AuditLog  # noqa: E402
from mcp_server import tools  # noqa: E402
from schema import introspect  # noqa: E402

from .test_mcp_http import payload, with_client  # noqa: E402

pytestmark = pytest.mark.no_default_access

MANAGED = ["master", "crm", "finance"]
ACCESS_TOOLS = {"get_user_access", "set_user_access"}


@pytest.fixture(autouse=True)
def managed(monkeypatch):
    names = list(MANAGED)
    monkeypatch.setattr(introspect, "managed_schemas",
                        lambda: [{"name": n, "tables": 0, "comment": None} for n in names])
    return names


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    cfg = {"auth": {"allowedDomains": [], "seedAdmins": [], "defaultRole": "viewer"}}
    monkeypatch.setattr(services, "get_config", lambda: cfg)


def make(email, role=Role.VIEWER, active=True):
    return User.objects.create(username=email, email=email, role=role, is_active=active)


def grants_of(u):
    return sorted(SchemaGrant.objects.filter(user=u).values_list("schema", "role"))


@pytest.fixture
def server():
    users = {r: make(f"{r}@example.com", r) for r in (Role.VIEWER, Role.EDITOR, Role.APPROVER, Role.ADMIN)}
    raw = {r: tokens.create_token(u, "e2e")[1] for r, u in users.items()}

    from mcp_server.app import build_app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started, "server did not start"
    yield {"url": f"http://127.0.0.1:{port}/mcp", "tokens": raw, "users": users}
    srv.should_exit = True
    thread.join(10)


def call(server, role, tool, args):
    return with_client(server["url"], server["tokens"][role], lambda c: c.call_tool(tool, args))


@pytest.mark.django_db(transaction=True)
def test_admin_token_lists_and_sets_access(server):
    target = make("t@example.com", Role.EDITOR)
    SchemaGrant.objects.create(user=target, schema="crm", role="viewer")

    names = {t.name for t in with_client(server["url"], server["tokens"][Role.ADMIN],
                                         lambda c: c.list_tools()).tools}
    assert ACCESS_TOOLS <= names

    got = payload(call(server, Role.ADMIN, "get_user_access", {"email": " T@Example.com "}))
    assert got == {"email": "t@example.com", "role": "editor", "cap": "editor",
                   "grants": [{"schema": "crm", "role": "viewer"}],
                   "databases": [{"name": "crm", "role": "viewer"}]}

    sent = [{"schema": "*", "role": "viewer"}, {"schema": "crm", "role": "editor"}, {"schema": "finance", "role": "none"}]
    out = payload(call(server, Role.ADMIN, "set_user_access", {"email": "t@example.com", "grants": sent}))
    assert out["email"] == "t@example.com" and out["changed"] is True
    assert out["before"] == [{"schema": "crm", "role": "viewer"}]
    assert out["after"] == [{"schema": "*", "role": "viewer"}, {"schema": "crm", "role": "editor"},
                            {"schema": "finance", "role": "none"}]
    assert out["databases"] == [{"name": "master", "role": "viewer"}, {"name": "crm", "role": "editor"}]
    assert grants_of(target) == [("*", "viewer"), ("crm", "editor"), ("finance", "none")]

    again = payload(call(server, Role.ADMIN, "get_user_access", {"email": "t@example.com"}))
    assert again["grants"] == out["after"] and again["databases"] == out["databases"]

    entry = AuditLog.objects.get(action="access.changed")
    assert entry.actor_email == "admin@example.com" and entry.target == "t@example.com"
    assert entry.details["via"] == "mcp"
    assert entry.details["before"] == [{"schema": "crm", "role": "viewer"}]
    assert entry.details["after"] == out["after"]

    # the same list again changes nothing and writes no second entry
    same = payload(call(server, Role.ADMIN, "set_user_access", {"email": "t@example.com", "grants": sent}))
    assert same["changed"] is False and same["before"] == same["after"]
    assert AuditLog.objects.filter(action="access.changed").count() == 1

    # refusals reach the AI as tool errors and change nothing
    res = call(server, Role.ADMIN, "set_user_access",
               {"email": "t@example.com", "grants": [{"schema": "crm", "role": "approver"}]})
    assert res.is_error and "above t@example.com's global role" in res.content[0].text
    res = call(server, Role.ADMIN, "get_user_access", {"email": "nobody@example.com"})
    assert res.is_error and res.content[0].text.endswith("no user with email 'nobody@example.com'")
    assert grants_of(target) == [("*", "viewer"), ("crm", "editor"), ("finance", "none")]


@pytest.mark.django_db(transaction=True)
def test_non_admin_tokens_never_see_access_tools(server):
    target = make("t@example.com", Role.EDITOR)
    for role in (Role.VIEWER, Role.EDITOR, Role.APPROVER):
        names = {t.name for t in with_client(server["url"], server["tokens"][role], lambda c: c.list_tools()).tools}
        assert not ACCESS_TOOLS & names, role
        for tool, args in (("get_user_access", {"email": "t@example.com"}),
                           ("set_user_access", {"email": "t@example.com",
                                                "grants": [{"schema": "*", "role": "editor"}]}),
                           ("set_user_access", {"email": f"{role}@example.com",
                                                "grants": [{"schema": "*", "role": role.value}]})):
            res = call(server, role, tool, args)
            assert res.is_error and "not available for your role" in res.content[0].text
    assert grants_of(target) == [] and not AuditLog.objects.filter(action="access.changed").exists()


@pytest.mark.django_db
def test_access_tools_are_admin_only_and_not_in_chat():
    from chat import toolspecs

    assert {t: tools.TOOL_ROLES[t] for t in ACCESS_TOOLS} == {"get_user_access": "admin", "set_user_access": "admin"}
    assert ACCESS_TOOLS <= toolspecs.NOT_IN_CHAT and not ACCESS_TOOLS & set(toolspecs.SPECS)
    for role in (Role.VIEWER, Role.EDITOR, Role.APPROVER):
        who = make(f"x-{role}@example.com", role)
        assert not any(tools.allowed(who, t) for t in ACCESS_TOOLS)
    boss = make("boss@example.com", Role.ADMIN)
    assert all(tools.allowed(boss, t) for t in ACCESS_TOOLS)


@pytest.mark.django_db
def test_the_functions_refuse_a_non_admin_even_when_called_directly():
    target = make("t@example.com", Role.EDITOR)
    for role in (Role.VIEWER, Role.EDITOR, Role.APPROVER):
        actor = make(f"a-{role}@example.com", role)
        with pytest.raises(tools.ToolFailure, match="only admins"):
            tools.get_user_access(actor, "t@example.com")
        with pytest.raises(tools.ToolFailure, match="only admins"):
            tools.set_user_access(actor, "t@example.com", [{"schema": "*", "role": "viewer"}])
    off = make("off@example.com", Role.ADMIN, active=False)
    with pytest.raises(tools.ToolFailure, match="only admins"):
        tools.set_user_access(off, "t@example.com", [{"schema": "*", "role": "viewer"}])
    assert grants_of(target) == []


@pytest.mark.django_db
@pytest.mark.parametrize("email", ["nobody@example.com", "not an email", "", None, 5])
def test_unknown_email_uses_the_lookup_text(email):
    admin = make("admin@example.com", Role.ADMIN)
    for fn in (lambda: tools.get_user_access(admin, email),
               lambda: tools.set_user_access(admin, email, [])):
        with pytest.raises(tools.ToolFailure) as exc:
            fn()
        assert str(exc.value) == f"no user with email {email!r}"


@pytest.mark.django_db
def test_set_user_access_validation_matches_web(client):
    admin = make("admin@example.com", Role.ADMIN)
    target = make("t@example.com", Role.EDITOR)
    boss = make("boss@example.com", Role.ADMIN)
    SchemaGrant.objects.create(user=target, schema="crm", role="viewer")
    client.force_login(admin)
    cases = [
        (target, [{"schema": "nope", "role": "viewer"}]),
        (target, [{"schema": "crm", "role": "admin"}]),
        (target, [{"schema": "crm", "role": "owner"}]),
        (target, [{"schema": "crm", "role": "approver"}]),
        (target, [{"schema": "*", "role": "approver"}]),
        (target, [{"schema": "*", "role": "none"}]),
        (target, [{"schema": "crm", "role": "viewer"}, {"schema": "crm", "role": "editor"}]),
        (target, [{"schema": "", "role": "viewer"}]),
        (target, [{"schema": "crm", "role": None}]),
        (target, [{"schema": ["crm"], "role": "viewer"}]),
        (target, ["crm"]),
        (target, "crm"),
        (target, None),
        (boss, [{"schema": "crm", "role": "viewer"}]),
    ]
    for who, grants in cases:
        web = client.put(f"/api/users/{who.pk}/access", data=json.dumps({"grants": grants}),
                         content_type="application/json")
        assert web.status_code == 400, (grants, web.content)
        with pytest.raises(tools.ToolFailure) as exc:
            tools.set_user_access(admin, who.email, grants)
        assert str(exc.value) == web.json()["error"], grants
    assert grants_of(target) == [("crm", "viewer")]
    assert not AuditLog.objects.filter(action="access.changed").exists()


@pytest.mark.django_db
def test_admin_target_reads_as_every_database():
    admin = make("admin@example.com", Role.ADMIN)
    boss = make("boss@example.com", Role.ADMIN)
    got = tools.get_user_access(admin, "boss@example.com")
    assert got == {"email": "boss@example.com", "role": "admin", "cap": "admin", "grants": [],
                   "databases": [{"name": n, "role": "admin"} for n in MANAGED]}
    out = tools.set_user_access(admin, boss.email, [])
    assert out["changed"] is False and out["after"] == []


@pytest.mark.django_db
def test_me_access_matches_effective_roles(client, managed):
    from accounts import access

    viewer = make("v@example.com", Role.VIEWER)
    SchemaGrant.objects.create(user=viewer, schema="crm", role="viewer")
    editor = make("e@example.com", Role.EDITOR)
    SchemaGrant.objects.create(user=editor, schema="*", role="editor")
    denied = make("d@example.com", Role.EDITOR)
    SchemaGrant.objects.create(user=denied, schema="*", role="editor")
    SchemaGrant.objects.create(user=denied, schema="finance", role="none")
    SchemaGrant.objects.create(user=denied, schema="crm", role="approver")  # above the cap: capped to editor
    nobody = make("n@example.com", Role.APPROVER)
    admin = make("a@example.com", Role.ADMIN)

    expected = {
        viewer: {"all": False, "schemas": [{"name": "crm", "role": "viewer"}]},
        editor: {"all": True, "schemas": [{"name": n, "role": "editor"} for n in MANAGED]},
        denied: {"all": False, "schemas": [{"name": "master", "role": "editor"}, {"name": "crm", "role": "editor"}]},
        nobody: {"all": False, "schemas": []},
        admin: {"all": True, "schemas": [{"name": n, "role": "admin"} for n in MANAGED]},
    }
    for user, want in expected.items():
        client.force_login(user)
        got = client.get("/api/me").json()["access"]
        assert got == want, user.email
        # what the profile shows is what the server enforces
        for name in MANAGED:
            role = access.effective_role(user, name)
            shown = next((s["role"] for s in got["schemas"] if s["name"] == name), None)
            assert shown == (str(role) if role is not None else None), (user.email, name)


@pytest.mark.django_db
def test_me_still_answers_when_the_databases_cannot_be_read(client, monkeypatch):
    def broken():
        raise RuntimeError("master is down")

    monkeypatch.setattr(introspect, "managed_schemas", broken)
    client.force_login(make("v@example.com"))
    r = client.get("/api/me")
    assert r.status_code == 200 and r.json()["access"] == {"all": False, "schemas": []}
