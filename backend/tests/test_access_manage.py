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

    # to admin: admin is never a grant, and the old grants are cleared (see the admin-promotion test)
    up = make("up@example.com", Role.APPROVER)
    SchemaGrant.objects.create(user=up, schema="*", role="approver")
    change_role(admin, up, "admin")
    assert grants_of(up) == []


def test_resending_get_output_is_a_no_op_whatever_the_collation(client, admin, managed, monkeypatch,
                                                                   django_capture_on_commit_callbacks):
    from django.db import connection

    with connection.cursor() as cur:  # a server whose default collation is not C (rolled back with the test)
        cur.execute('ALTER TABLE accounts_schemagrant ALTER COLUMN schema TYPE varchar(63) COLLATE "en-x-icu"')
    managed[:] = ["master", "Zeta", "alpha", "crm_eu", "crmeu", "B-side", "b_side"]
    closed = []
    monkeypatch.setattr(events, "close_user_sockets", closed.append)
    target = make("t@example.com", Role.EDITOR)
    sent = [{"schema": n, "role": "viewer"} for n in managed] + [{"schema": "*", "role": "editor"}]
    client.force_login(admin)
    with django_capture_on_commit_callbacks(execute=True):
        assert put(client, target.pk, {"grants": sent}).status_code == 200
    assert closed == [target.pk]
    got = client.get(f"/api/users/{target.pk}/access").json()["grants"]
    assert got[0] == {"schema": "*", "role": "editor"}
    assert [g["schema"] for g in got[1:]] == sorted(managed)  # code-point order, not the database's collation
    ids = sorted(SchemaGrant.objects.filter(user=target).values_list("pk", flat=True))
    for body in (got, list(reversed(got))):
        with django_capture_on_commit_callbacks(execute=True):
            assert put(client, target.pk, {"grants": body}).status_code == 200
    assert closed == [target.pk]
    assert AuditLog.objects.filter(action="access.changed").count() == 1
    assert sorted(SchemaGrant.objects.filter(user=target).values_list("pk", flat=True)) == ids  # no row churn
    # a real change keeps the untouched rows and records only one entry
    with django_capture_on_commit_callbacks(execute=True):
        assert put(client, target.pk, {"grants": got[:-1]}).status_code == 200
    assert AuditLog.objects.filter(action="access.changed").count() == 2
    assert set(SchemaGrant.objects.filter(user=target).values_list("pk", flat=True)) < set(ids)


def test_promotion_to_admin_clears_grants_and_demotion_does_not_revive_them(client, admin):
    target = make("t@example.com", Role.APPROVER)
    SchemaGrant.objects.create(user=target, schema="*", role="approver")
    SchemaGrant.objects.create(user=target, schema="crm", role="viewer")
    change_role(admin, target, "admin")
    assert grants_of(target) == []
    details = AuditLog.objects.get(action="user.role_changed", target="t@example.com").details
    assert details["access_cleared"] == [{"schema": "*", "role": "approver"}, {"schema": "crm", "role": "viewer"}]
    assert user_access(target) == {"grants": [], "cap": "admin"}
    change_role(admin, target, "editor")
    assert grants_of(target) == [] and user_access(target) == {"grants": [], "cap": "editor"}
    from accounts import access
    assert access.readable_schemas(target) == []

    plain = make("p@example.com", Role.VIEWER)
    change_role(admin, plain, "admin")
    assert "access_cleared" not in AuditLog.objects.get(action="user.role_changed", target="p@example.com").details


def test_admin_target_stale_rows_hidden_and_empty_put_is_a_no_op(client, admin):
    boss = make("boss@example.com", Role.ADMIN)
    SchemaGrant.objects.create(user=boss, schema="crm", role="viewer")  # left over from before the ruling
    assert user_access(boss) == {"grants": [], "cap": "admin"}
    client.force_login(admin)
    users = {u["email"]: u for u in client.get("/api/users").json()["users"]}
    assert users["boss@example.com"]["access"] == {"all": True, "schemas": []}
    res = put(client, boss.pk, {"grants": []})
    assert res.status_code == 200 and res.json() == {"grants": [], "cap": "admin"}
    assert not AuditLog.objects.filter(action="access.changed").exists()
    assert grants_of(boss) == [("crm", "viewer")]  # clears nothing
    assert put(client, boss.pk, {"grants": [{"schema": "*", "role": "viewer"}]}).status_code == 400


def test_actor_demoted_before_the_lock_cannot_write(admin, monkeypatch):
    target = make("t@example.com", Role.EDITOR)
    stale_copy = User.objects.get(pk=admin.pk)
    User.objects.filter(pk=admin.pk).update(role=Role.VIEWER)  # demoted after the request loaded the actor
    with pytest.raises(NotAdmin):
        set_user_access(stale_copy, target, [{"schema": "crm", "role": "editor"}])
    assert grants_of(target) == []


def test_unknown_database_name_is_truncated_in_the_error(admin):
    target = make("t@example.com", Role.EDITOR)
    with pytest.raises(UserManagementDenied) as exc:
        set_user_access(admin, target, [{"schema": "x" * 5000, "role": "viewer"}])
    assert len(str(exc.value)) < 120 and "…" in str(exc.value)


def test_promotion_over_two_levels_and_without_a_star_grant(admin):
    v = make("v@example.com", Role.VIEWER)
    SchemaGrant.objects.create(user=v, schema="*", role="viewer")
    change_role(admin, v, "approver")
    assert grants_of(v) == [("*", "approver")]
    none = make("none@example.com", Role.VIEWER)
    SchemaGrant.objects.create(user=none, schema="crm", role="viewer")
    change_role(admin, none, "editor")
    assert grants_of(none) == [("crm", "viewer")]
    assert "access_star_raised" not in AuditLog.objects.get(action="user.role_changed", target="none@example.com").details
