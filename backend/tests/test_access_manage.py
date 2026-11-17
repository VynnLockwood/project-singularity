"""Admins manage per-database grants: the service, the web endpoints, initial access and role promotion."""
import json

import pytest

from accounts import services
from accounts.models import Role, SchemaGrant, User
from accounts.services import NotAdmin, UserManagementDenied, change_role, create_user, set_user_access, user_access
from audit.models import AuditLog
from chat import events
from schema import introspect

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_access]

MANAGED = ["master", "crm", "finance"]


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


@pytest.fixture
def admin():
    return make("admin@example.com", Role.ADMIN)


def grants_of(u):
    return sorted(SchemaGrant.objects.filter(user=u).values_list("schema", "role"))


def put(client, uid, body):
    return client.put(f"/api/users/{uid}/access", data=json.dumps(body), content_type="application/json")


def test_only_admin_reads_or_writes_access(client):
    target = make("t@example.com", Role.APPROVER)
    SchemaGrant.objects.create(user=target, schema="crm", role="viewer")
    assert client.get(f"/api/users/{target.pk}/access").status_code == 401
    assert put(client, target.pk, {"grants": []}).status_code == 401
    for role in (Role.VIEWER, Role.EDITOR, Role.APPROVER):
        actor = make(f"{role.value}@example.com", role)
        client.force_login(actor)
        assert client.get(f"/api/users/{target.pk}/access").status_code == 403
        res = put(client, target.pk, {"grants": [{"schema": "*", "role": "approver"}]})
        assert res.status_code == 403
        with pytest.raises(NotAdmin):
            set_user_access(actor, target, [{"schema": "*", "role": "viewer"}])
        with pytest.raises(NotAdmin):
            set_user_access(actor, target, [], via="mcp")
    assert grants_of(target) == [("crm", "viewer")]
    assert not AuditLog.objects.filter(action="access.changed").exists()


def test_cannot_grant_self_when_not_admin(client):
    me = make("me@example.com", Role.APPROVER)
    client.force_login(me)
    assert put(client, me.pk, {"grants": [{"schema": "*", "role": "approver"}]}).status_code == 403
    with pytest.raises(NotAdmin):
        set_user_access(me, me, [{"schema": "*", "role": "approver"}])
    assert grants_of(me) == []


def test_put_replaces_grants_atomically_and_audits(client, admin):
    target = make("t@example.com", Role.EDITOR)
    SchemaGrant.objects.create(user=target, schema="*", role="viewer")
    SchemaGrant.objects.create(user=target, schema="crm", role="editor")
    client.force_login(admin)
    res = put(client, target.pk, {"grants": [{"schema": "finance", "role": "editor"}, {"schema": "*", "role": "editor"}]})
    assert res.status_code == 200, res.content
    assert res.json() == {"grants": [{"schema": "*", "role": "editor"}, {"schema": "finance", "role": "editor"}],
                          "cap": "editor"}
    assert grants_of(target) == [("*", "editor"), ("finance", "editor")]
    entry = AuditLog.objects.get(action="access.changed")
    assert entry.actor_email == "admin@example.com" and entry.target == "t@example.com"
    assert entry.details["before"] == [{"schema": "*", "role": "viewer"}, {"schema": "crm", "role": "editor"}]
    assert entry.details["after"] == [{"schema": "*", "role": "editor"}, {"schema": "finance", "role": "editor"}]
    assert entry.details["via"] == "web"
    # a refused list in the middle changes nothing (one bad entry, the good ones are not half applied)
    res = put(client, target.pk, {"grants": [{"schema": "crm", "role": "viewer"}, {"schema": "nope", "role": "viewer"}]})
    assert res.status_code == 400
    assert grants_of(target) == [("*", "editor"), ("finance", "editor")]
    assert AuditLog.objects.filter(action="access.changed").count() == 1


def test_rejects_unknown_schema_bad_role_admin_target_and_above_cap(client, admin):
    target = make("t@example.com", Role.EDITOR)
    SchemaGrant.objects.create(user=target, schema="crm", role="viewer")
    other_admin = make("a2@example.com", Role.ADMIN)
    client.force_login(admin)
    cases = [
        (target, {"grants": [{"schema": "nope", "role": "viewer"}]}, "nope"),
        (target, {"grants": [{"schema": "crm", "role": "admin"}]}, "role"),
        (target, {"grants": [{"schema": "crm", "role": "owner"}]}, "role"),
        (target, {"grants": [{"schema": "crm", "role": "approver"}]}, "global role"),
        (target, {"grants": [{"schema": "*", "role": "none"}]}, "All databases"),
        (target, {"grants": [{"schema": "crm", "role": "viewer"}, {"schema": "crm", "role": "editor"}]}, "twice"),
        (other_admin, {"grants": [{"schema": "*", "role": "viewer"}]}, "admins reach every database"),
        (target, {}, "grants"),
        (target, {"grants": "crm"}, "list"),
        (target, {"grants": ["crm"]}, "schema"),
        (target, {"grants": [{"schema": 3, "role": "viewer"}]}, "schema"),
        (target, {"grants": [{"schema": "crm", "role": None}]}, "role"),
        (target, {"grants": [{"schema": ["crm"], "role": "viewer"}]}, "schema"),
        (target, {"grants": [{"role": "viewer"}]}, "schema"),
    ]
    for who, body, reason in cases:
        res = put(client, who.pk, body)
        assert res.status_code == 400, (body, res.content)
        assert reason in res.json()["error"], (body, res.json())
    assert grants_of(target) == [("crm", "viewer")]
    assert grants_of(other_admin) == []
    assert not AuditLog.objects.filter(action="access.changed").exists()
    # the service refuses non-list input too, never a crash
    for bad in (None, "x", {"schema": "crm"}, [None], [{"schema": "crm", "role": 1}]):
        with pytest.raises(UserManagementDenied):
            set_user_access(admin, target, bad)
    with pytest.raises(UserManagementDenied):
        set_user_access(admin, target, [], via="carrier pigeon")
    assert put(client, 999999, {"grants": []}).status_code == 404
    assert client.get("/api/users/999999/access").status_code == 404


def test_star_and_specific_grants_roundtrip(client, admin):
    target = make("t@example.com", Role.APPROVER)
    client.force_login(admin)
    assert client.get(f"/api/users/{target.pk}/access").json() == {"grants": [], "cap": "approver"}
    sent = [{"schema": "*", "role": "editor"}, {"schema": "crm", "role": "approver"},
            {"schema": "finance", "role": "none"}]
    assert put(client, target.pk, {"grants": sent}).status_code == 200
    got = client.get(f"/api/users/{target.pk}/access").json()
    assert got == {"grants": sorted(sent, key=lambda g: g["schema"]), "cap": "approver"}
    # sending back what GET returned is a no-op: no new audit entry
    assert put(client, target.pk, {"grants": got["grants"]}).status_code == 200
    assert AuditLog.objects.filter(action="access.changed").count() == 1
    # the list shows a summary
    users = {u["email"]: u for u in client.get("/api/users").json()["users"]}
    assert users["t@example.com"]["access"] == {
        "all": True, "schemas": [{"name": "crm", "role": "approver"}, {"name": "finance", "role": "none"}]}
    assert users["admin@example.com"]["access"] == {"all": True, "schemas": []}
    assert put(client, target.pk, {"grants": []}).status_code == 200
    users = {u["email"]: u for u in client.get("/api/users").json()["users"]}
    assert users["t@example.com"]["access"] == {"all": False, "schemas": []}


def test_stale_grants_are_flagged_and_dropped_on_replace(client, admin, managed):
    target = make("t@example.com", Role.EDITOR)
    SchemaGrant.objects.create(user=target, schema="gone", role="viewer")
    SchemaGrant.objects.create(user=target, schema="crm", role="viewer")
    assert user_access(target) == {"grants": [{"schema": "crm", "role": "viewer"},
                                              {"schema": "gone", "role": "viewer", "stale": True}],
                                   "cap": "editor"}
    client.force_login(admin)
    # re-sending a stale one is refused (it is not a managed database any more)
    assert put(client, target.pk, {"grants": [{"schema": "gone", "role": "viewer"}]}).status_code == 400
    # not re-sending it drops it; the "stale" flag GET returned is accepted and ignored
    res = put(client, target.pk, {"grants": [{"schema": "crm", "role": "editor", "stale": False}]})
    assert res.status_code == 200
    assert grants_of(target) == [("crm", "editor")]


def test_grant_change_closes_target_sockets(admin, monkeypatch, django_capture_on_commit_callbacks):
    closed = []
    monkeypatch.setattr(events, "close_user_sockets", closed.append)
    target = make("t@example.com", Role.EDITOR)
    with django_capture_on_commit_callbacks(execute=True):
        set_user_access(admin, target, [{"schema": "crm", "role": "editor"}], via="mcp")
    assert closed == [target.pk]
    assert AuditLog.objects.get(action="access.changed").details["via"] == "mcp"
    with django_capture_on_commit_callbacks(execute=True):  # nothing changed: no close, no audit
        set_user_access(admin, target, [{"schema": "crm", "role": "editor"}])
    assert closed == [target.pk]
    with django_capture_on_commit_callbacks(execute=True):  # refused: nothing closed
        with pytest.raises(UserManagementDenied):
            set_user_access(admin, target, [{"schema": "crm", "role": "approver"}])
    assert closed == [target.pk]


def test_create_user_with_initial_access(client, admin):
    user = create_user(admin, "new@example.com", "editor", access=[{"schema": "crm", "role": "editor"}])
    assert grants_of(user) == [("crm", "editor")]
    entry = AuditLog.objects.get(action="access.changed")
    assert entry.target == "new@example.com" and entry.details["before"] == []
    # an invalid grant rolls back the whole creation
    with pytest.raises(UserManagementDenied):
        create_user(admin, "bad@example.com", "viewer", access=[{"schema": "crm", "role": "editor"}])
    with pytest.raises(UserManagementDenied):
        create_user(admin, "bad@example.com", "viewer", access="crm")
    with pytest.raises(UserManagementDenied):
        create_user(admin, "bad@example.com", "admin", access=[{"schema": "*", "role": "viewer"}])
    assert not User.objects.filter(email="bad@example.com").exists()
    assert not AuditLog.objects.filter(target="bad@example.com").exists()
    # the web form passes access through
    client.force_login(admin)
    res = client.post("/api/users", data=json.dumps({"email": "web@example.com", "role": "viewer",
                                                     "access": [{"schema": "*", "role": "viewer"}]}),
                      content_type="application/json")
    assert res.status_code == 201, res.content
    assert res.json()["access"] == {"all": True, "schemas": []}
    res = client.post("/api/users", data=json.dumps({"email": "web2@example.com", "role": "viewer",
                                                     "access": [{"schema": "nope", "role": "viewer"}]}),
                      content_type="application/json")
    assert res.status_code == 400
    assert not User.objects.filter(email="web2@example.com").exists()


def test_new_user_without_access_has_no_grants(admin):
    user = create_user(admin, "new@example.com", "approver")
    assert grants_of(user) == []
    assert user_access(user) == {"grants": [], "cap": "approver"}
    assert not AuditLog.objects.filter(action="access.changed").exists()
    assert create_user(admin, "empty@example.com", "admin", access=[]).role == Role.ADMIN


def test_promotion_raises_star_grant_only_when_it_matched_the_old_role(admin):
    viewer = make("v@example.com", Role.VIEWER)
    SchemaGrant.objects.create(user=viewer, schema="*", role="viewer")
    SchemaGrant.objects.create(user=viewer, schema="crm", role="viewer")
    change_role(admin, viewer, "editor")
    assert grants_of(viewer) == [("*", "editor"), ("crm", "viewer")]  # specific grants stay
    details = AuditLog.objects.filter(action="user.role_changed", target="v@example.com").get().details
    assert details["access_star_raised"] == {"from": "viewer", "to": "editor"}

    narrowed = make("n@example.com", Role.EDITOR)
    SchemaGrant.objects.create(user=narrowed, schema="*", role="viewer")
    change_role(admin, narrowed, "approver")
    assert grants_of(narrowed) == [("*", "viewer")]
    assert "access_star_raised" not in AuditLog.objects.get(action="user.role_changed", target="n@example.com").details

    # lowering changes no grants (the global role caps them)
    change_role(admin, viewer, "viewer")
    assert grants_of(viewer) == [("*", "editor"), ("crm", "viewer")]

    # to admin: admin is never a grant
    up = make("up@example.com", Role.APPROVER)
    SchemaGrant.objects.create(user=up, schema="*", role="approver")
    change_role(admin, up, "admin")
    assert grants_of(up) == [("*", "approver")]
