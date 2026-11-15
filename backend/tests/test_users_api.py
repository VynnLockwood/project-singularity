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
    assert set(by["off@example.com"]) == {"id", "email", "name", "role", "active", "lastLoginAt"}
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
        "Manage users and roles",
    ]
    tool = next(t for t in me["capabilities"]["mcpTools"] if t["name"] == "approve_change")
    assert tool["minRole"] == "approver" and tool["allowed"] is False and tool["description"]


def test_every_tool_is_listed():
    from accounts import capabilities
    listed = {t["name"] for t in capabilities.build(make("v@example.com"))["mcpTools"]}
    assert listed == set(tools.TOOL_ROLES)
