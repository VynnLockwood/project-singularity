"""Users endpoints (admin only) and the capabilities in /api/me."""
import json

import pytest

from accounts import services
from accounts.models import Role, User
from mcp_server import tools

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    cfg = {"auth": {"allowedDomains": [], "seedAdmins": [], "defaultRole": "viewer"}}
    monkeypatch.setattr(services, "get_config", lambda: cfg)


def make(email, role=Role.VIEWER, active=True):
    return User.objects.create(username=email, email=email, role=role, is_active=active)


def send(client, method, url, body=None):
    if method == "get":
        return client.get(url)
    return getattr(client, method)(url, data=json.dumps(body or {}), content_type="application/json")


def test_users_endpoints_need_admin(client):
    target = make("t@example.com")
    for method, url in (("get", "/api/users"), ("post", "/api/users"), ("patch", f"/api/users/{target.pk}")):
        assert send(client, method, url).status_code == 401
    for role in (Role.VIEWER, Role.EDITOR, Role.APPROVER):
        client.force_login(make(f"{role.value}@example.com", role))
        for method, url in (("get", "/api/users"), ("post", "/api/users"), ("patch", f"/api/users/{target.pk}")):
            assert send(client, method, url, {"email": "x@example.com", "role": "viewer"}).status_code == 403
        assert send(client, "patch", f"/api/users/{target.pk}/role", {"role": "editor"}).status_code == 403
    target.refresh_from_db()
    assert target.role == Role.VIEWER


def test_admin_lists_creates_patches_users(client):
    admin = make("admin@example.com", Role.ADMIN)
    off = make("off@example.com", active=False)
    client.force_login(admin)
    users = client.get("/api/users").json()["users"]
    by = {u["email"]: u for u in users}
    assert set(by) == {"admin@example.com", "off@example.com"}
    assert by["off@example.com"]["active"] is False
    assert set(by["off@example.com"]) == {"id", "email", "name", "role", "active", "lastLoginAt", "access"}
    assert by["admin@example.com"]["lastLoginAt"] is not None  # force_login sets last_login
    assert by["off@example.com"]["lastLoginAt"] is None

    res = send(client, "post", "/api/users", {"email": " New@Example.com ", "role": "editor", "name": "Ada Lovelace"})
    assert res.status_code == 201
    body = res.json()
    assert (body["email"], body["role"], body["active"], body["name"]) == ("new@example.com", "editor", True, "Ada Lovelace")

    res = send(client, "patch", f"/api/users/{body['id']}", {"role": "approver", "active": False})
    assert res.status_code == 200
    assert (res.json()["role"], res.json()["active"]) == ("approver", False)
    res = send(client, "patch", f"/api/users/{off.pk}", {"active": True})
    assert res.json()["active"] is True
    assert send(client, "patch", f"/api/users/{off.pk}/role", {"role": "editor"}).status_code == 200


def test_users_validation(client):
    admin = make("admin@example.com", Role.ADMIN)
    target = make("t@example.com")
    client.force_login(admin)
    assert send(client, "post", "/api/users", {"email": "nope", "role": "viewer"}).status_code == 400
    assert send(client, "post", "/api/users", {"email": "a@example.com", "role": "king"}).status_code == 400
    assert send(client, "post", "/api/users", {"email": "T@example.com", "role": "viewer"}).status_code == 400
    assert send(client, "patch", "/api/users/999999", {"role": "editor"}).status_code == 404
    assert send(client, "patch", f"/api/users/{admin.pk}", {"role": "viewer"}).status_code == 400
    assert send(client, "patch", f"/api/users/{target.pk}", {"role": "bogus"}).status_code == 400
    assert send(client, "patch", f"/api/users/{target.pk}", {}).status_code == 400
    assert send(client, "patch", f"/api/users/{target.pk}", {"active": "yes"}).status_code == 400


def test_me_has_role_summary(client):
    client.force_login(make("v@example.com"))
    me = client.get("/api/me").json()
    assert me["active"] is True
    assert me["capabilities"]["roleSummary"] == [
        "Read data and the schema",
        "Edit data, import files, propose schema changes",
        "Approve changes, settings, backups",
        "Manage users, roles and who can reach each database",
    ]
    tool = next(t for t in me["capabilities"]["mcpTools"] if t["name"] == "approve_change")
    assert tool["minRole"] == "approver" and tool["allowed"] is False and tool["description"]


def test_every_tool_is_listed():
    from accounts import capabilities
    listed = {t["name"]: t for t in capabilities.build(make("v@example.com"))["mcpTools"]}
    assert set(listed) == set(tools.TOOL_ROLES)
    assert {"get_user_access", "set_user_access"} <= set(listed)
    for name in ("get_user_access", "set_user_access"):
        assert listed[name]["minRole"] == "admin" and listed[name]["allowed"] is False and listed[name]["description"]
    admin = {t["name"]: t for t in capabilities.build(make("a@example.com", Role.ADMIN))["mcpTools"]}
    assert set(admin) == set(listed) and all(t["allowed"] for t in admin.values() if t["name"] != "propose_sql")


# ---- fix round 1 -------------------------------------------------------------

def test_not_admin_maps_to_403_whatever_the_text_and_conflicts_stay_400():
    from accounts import views
    assert views._refused(services.NotAdmin("x")).status_code == 403
    assert views._refused(services.UserManagementDenied("only admins can manage users")).status_code == 400
    assert views._refused(services.UserManagementDenied("cannot remove the last active admin")).status_code == 400


@pytest.mark.parametrize("bad", [[], {}, 5, None])
def test_non_string_role_is_400_not_500(client, bad):
    client.force_login(make("admin@example.com", Role.ADMIN))
    target = make("t@example.com")
    assert send(client, "post", "/api/users", {"email": "n@example.com", "role": bad}).status_code == 400
    assert not User.objects.filter(email="n@example.com").exists()
    assert send(client, "patch", f"/api/users/{target.pk}/role", {"role": bad}).status_code == 400
    if bad is not None:  # null on PATCH /api/users/<id> means "leave the role alone"
        assert send(client, "patch", f"/api/users/{target.pk}", {"role": bad, "active": True}).status_code == 400
    target.refresh_from_db()
    assert target.role == Role.VIEWER


def test_self_deactivate_and_self_demote_are_400(client):
    admin = make("admin@example.com", Role.ADMIN)
    client.force_login(admin)
    assert send(client, "patch", f"/api/users/{admin.pk}", {"active": False}).status_code == 400
    assert send(client, "patch", f"/api/users/{admin.pk}", {"role": "viewer"}).status_code == 400
    admin.refresh_from_db()
    assert admin.role == Role.ADMIN and admin.is_active


def test_last_active_admin_guard_is_reached_and_maps_to_400():
    """A stale in-memory admin actor (its DB row already demoted) gets past the admin check, so the service
    reaches the last-admin guard; the view helper maps that refusal to exactly 400."""
    from accounts import views
    actor, last = make("a@example.com", Role.ADMIN), make("l@example.com", Role.ADMIN)
    User.objects.filter(pk=actor.pk).update(role=Role.VIEWER)  # actor object in memory still says admin
    for act in (lambda: services.set_user_active(actor, last, False), lambda: services.change_role(actor, last, "viewer")):
        with pytest.raises(services.UserManagementDenied, match="last active admin") as exc:
            act()
        assert not isinstance(exc.value, services.NotAdmin)
        assert views._refused(exc.value).status_code == 400
    last.refresh_from_db()
    assert last.role == Role.ADMIN and last.is_active


def test_patch_is_atomic(client):
    client.force_login(make("a@example.com", Role.ADMIN))
    t = make("t@example.com", Role.EDITOR)
    from unittest import mock
    with mock.patch("accounts.views.set_user_active", side_effect=services.UserManagementDenied("boom")):
        res = send(client, "patch", f"/api/users/{t.pk}", {"role": "approver", "active": False})
    assert res.status_code == 400
    t.refresh_from_db()
    assert t.role == Role.EDITOR


def test_propose_sql_hidden_when_sql_disabled(client, monkeypatch):
    monkeypatch.setattr(tools, "get_config", lambda: {"sql": {"enabled": False}})
    client.force_login(make("adm@example.com", Role.ADMIN))
    me = client.get("/api/me").json()["capabilities"]["mcpTools"]
    assert next(t for t in me if t["name"] == "propose_sql")["allowed"] is False
    assert tools.allowed(User.objects.get(email="adm@example.com"), "propose_sql") is False  # what tools/list filters on
