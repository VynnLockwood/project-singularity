import json
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import Client

from accounts import google
from accounts.models import Role, User
from accounts.services import RoleChangeDenied, SignInDenied, change_role, sign_in_from_claims
from audit.models import AuditLog

CFG = {
    "allowedDomains": ["example.com"],
    "seedAdmins": ["boss@example.com"],
    "defaultRole": "viewer",
}


def claims(email, verified=True, **extra):
    return {"email": email, "email_verified": verified, **extra}


def make_user(email, role=Role.VIEWER):
    return User.objects.create(username=email, email=email, role=role)


# ---- sign-in service -------------------------------------------------------

@pytest.mark.django_db
def test_new_user_gets_default_role():
    user = sign_in_from_claims(claims("new@example.com", given_name="Ann"), CFG)
    assert user.role == Role.VIEWER
    assert user.first_name == "Ann"
    assert not user.has_usable_password()


@pytest.mark.django_db
def test_seed_admin_becomes_approver_on_first_sign_in():
    user = sign_in_from_claims(claims("Boss@Example.com"), CFG)
    assert user.email == "boss@example.com"
    assert user.role == Role.APPROVER


@pytest.mark.django_db
def test_default_role_comes_from_config():
    user = sign_in_from_claims(claims("e@example.com"), {**CFG, "defaultRole": "editor"})
    assert user.role == Role.EDITOR


@pytest.mark.django_db
def test_wrong_domain_rejected():
    with pytest.raises(SignInDenied):
        sign_in_from_claims(claims("x@other.com"), CFG)
    assert not User.objects.filter(email="x@other.com").exists()


@pytest.mark.django_db
def test_lookalike_domain_rejected():
    with pytest.raises(SignInDenied):
        sign_in_from_claims(claims("x@evil-example.com"), CFG)
    with pytest.raises(SignInDenied):
        sign_in_from_claims(claims("x@example.com.evil.io"), CFG)


@pytest.mark.django_db
def test_unverified_email_rejected():
    with pytest.raises(SignInDenied):
        sign_in_from_claims(claims("a@example.com", verified=False), CFG)


@pytest.mark.django_db
def test_no_allowed_domains_means_nobody_can_sign_in():
    with pytest.raises(SignInDenied):
        sign_in_from_claims(claims("a@example.com"), {**CFG, "allowedDomains": []})


@pytest.mark.django_db
def test_existing_user_keeps_role_on_later_sign_in():
    u = make_user("keep@example.com", Role.EDITOR)
    again = sign_in_from_claims(claims("keep@example.com"), CFG)
    assert again.pk == u.pk and again.role == Role.EDITOR


@pytest.mark.django_db
def test_disabled_account_rejected():
    u = make_user("off@example.com")
    u.is_active = False
    u.save()
    with pytest.raises(SignInDenied):
        sign_in_from_claims(claims("off@example.com"), CFG)


@pytest.mark.django_db
def test_sign_in_is_audited():
    sign_in_from_claims(claims("a@example.com"), CFG)
    actions = list(AuditLog.objects.values_list("action", flat=True))
    assert "user.created" in actions and "user.signed_in" in actions


# ---- role helpers ----------------------------------------------------------

@pytest.mark.parametrize(
    "role,read,edit,approve",
    [
        (Role.VIEWER, True, False, False),
        (Role.EDITOR, True, True, False),
        (Role.APPROVER, True, True, True),
    ],
)
def test_role_permissions(role, read, edit, approve):
    u = User(role=role)
    assert (u.can_read, u.can_edit, u.can_approve) == (read, edit, approve)


# ---- role changes ----------------------------------------------------------

@pytest.mark.django_db
def test_approver_can_change_role_and_it_is_audited():
    admin = make_user("admin@example.com", Role.APPROVER)
    target = make_user("t@example.com")
    change_role(admin, target, "editor")
    target.refresh_from_db()
    assert target.role == Role.EDITOR
    entry = AuditLog.objects.get(action="user.role_changed")
    assert entry.actor_email == "admin@example.com"
    assert entry.target == "t@example.com"
    assert entry.details == {"from": "viewer", "to": "editor"}


@pytest.mark.django_db
@pytest.mark.parametrize("role", [Role.VIEWER, Role.EDITOR])
def test_non_approver_cannot_change_role(role):
    actor = make_user("a@example.com", role)
    target = make_user("t@example.com")
    with pytest.raises(RoleChangeDenied):
        change_role(actor, target, "editor")
    target.refresh_from_db()
    assert target.role == Role.VIEWER


@pytest.mark.django_db
def test_unknown_role_rejected():
    admin = make_user("admin@example.com", Role.APPROVER)
    with pytest.raises(RoleChangeDenied):
        change_role(admin, make_user("t@example.com"), "superuser")


@pytest.mark.django_db
def test_cannot_remove_last_approver():
    admin = make_user("admin@example.com", Role.APPROVER)
    with pytest.raises(RoleChangeDenied):
        change_role(admin, admin, "viewer")
    admin.refresh_from_db()
    assert admin.role == Role.APPROVER


@pytest.mark.django_db
def test_can_demote_an_approver_when_another_remains():
    a = make_user("a@example.com", Role.APPROVER)
    b = make_user("b@example.com", Role.APPROVER)
    change_role(a, b, "editor")
    b.refresh_from_db()
    assert b.role == Role.EDITOR


# ---- HTTP views ------------------------------------------------------------

@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    cfg = {"auth": CFG}
    monkeypatch.setattr("accounts.views.get_config", lambda: cfg)


@pytest.mark.django_db
def test_me_requires_login(client):
    assert client.get("/api/me").status_code == 401


@pytest.mark.django_db
def test_me_returns_user(client):
    u = make_user("me@example.com", Role.EDITOR)
    client.force_login(u)
    data = client.get("/api/me").json()
    assert data["email"] == "me@example.com" and data["role"] == "editor"


@pytest.mark.django_db
def test_login_redirects_to_google_with_state(client, settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = "cid"
    resp = client.get("/api/auth/google/login")
    assert resp.status_code == 302
    url = urlparse(resp["Location"])
    q = parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert q["client_id"] == ["cid"]
    assert q["state"] == [client.session["google_oauth_state"]]
    assert q["hd"] == ["example.com"]
    assert q["redirect_uri"][0].endswith("/api/auth/google/callback")


@pytest.mark.django_db
def test_callback_signs_in_and_sets_session(client, monkeypatch):
    client.get("/api/auth/google/login")
    state = client.session["google_oauth_state"]
    monkeypatch.setattr(google, "fetch_claims", lambda code, uri: claims("boss@example.com"))
    resp = client.get("/api/auth/google/callback", {"code": "abc", "state": state})
    assert resp.status_code == 302 and resp["Location"] == "/"
    assert client.get("/api/me").json()["role"] == "approver"


@pytest.mark.django_db
def test_callback_rejects_bad_state(client, monkeypatch):
    client.get("/api/auth/google/login")
    monkeypatch.setattr(google, "fetch_claims", lambda c, u: claims("boss@example.com"))
    resp = client.get("/api/auth/google/callback", {"code": "abc", "state": "wrong"})
    assert resp.status_code == 400
    assert client.get("/api/me").status_code == 401


@pytest.mark.django_db
def test_callback_without_prior_login_rejected(client):
    resp = client.get("/api/auth/google/callback", {"code": "abc", "state": "x"})
    assert resp.status_code == 400


@pytest.mark.django_db
def test_callback_wrong_domain_forbidden(client, monkeypatch):
    client.get("/api/auth/google/login")
    state = client.session["google_oauth_state"]
    monkeypatch.setattr(google, "fetch_claims", lambda c, u: claims("x@other.com"))
    resp = client.get("/api/auth/google/callback", {"code": "abc", "state": state})
    assert resp.status_code == 403
    assert client.get("/api/me").status_code == 401


@pytest.mark.django_db
def test_callback_google_failure_is_400(client, monkeypatch):
    client.get("/api/auth/google/login")
    state = client.session["google_oauth_state"]

    def boom(code, uri):
        raise google.GoogleAuthError("nope")

    monkeypatch.setattr(google, "fetch_claims", boom)
    assert client.get("/api/auth/google/callback", {"code": "a", "state": state}).status_code == 400


@pytest.mark.django_db
def test_state_cannot_be_replayed(client, monkeypatch):
    client.get("/api/auth/google/login")
    state = client.session["google_oauth_state"]
    monkeypatch.setattr(google, "fetch_claims", lambda c, u: claims("a@example.com"))
    assert client.get("/api/auth/google/callback", {"code": "a", "state": state}).status_code == 302
    assert client.get("/api/auth/google/callback", {"code": "a", "state": state}).status_code == 400


@pytest.mark.django_db
def test_logout(client):
    client.force_login(make_user("a@example.com"))
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/me").status_code == 401


@pytest.mark.django_db
def test_users_list_permissions(client):
    make_user("v@example.com", Role.VIEWER)
    assert client.get("/api/users").status_code == 401
    client.force_login(User.objects.get(email="v@example.com"))
    assert client.get("/api/users").status_code == 403
    admin = make_user("admin@example.com", Role.APPROVER)
    client.force_login(admin)
    emails = [u["email"] for u in client.get("/api/users").json()["users"]]
    assert emails == ["admin@example.com", "v@example.com"]


def patch_role(client, user_id, role):
    return client.patch(
        f"/api/users/{user_id}/role", data=json.dumps({"role": role}), content_type="application/json"
    )


@pytest.mark.django_db
def test_role_change_endpoint(client):
    target = make_user("t@example.com")
    assert patch_role(client, target.pk, "editor").status_code == 401

    client.force_login(make_user("e@example.com", Role.EDITOR))
    assert patch_role(client, target.pk, "editor").status_code == 403

    client.force_login(make_user("admin@example.com", Role.APPROVER))
    assert patch_role(client, target.pk, "editor").status_code == 200
    target.refresh_from_db()
    assert target.role == Role.EDITOR
    assert patch_role(client, target.pk, "bogus").status_code == 400
    assert patch_role(client, 999999, "editor").status_code == 404


@pytest.mark.django_db
def test_csrf_enforced_on_role_change():
    admin = make_user("admin@example.com", Role.APPROVER)
    target = make_user("t@example.com")
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(admin)
    resp = strict.patch(
        f"/api/users/{target.pk}/role", data=json.dumps({"role": "editor"}), content_type="application/json"
    )
    assert resp.status_code == 403
    target.refresh_from_db()
    assert target.role == Role.VIEWER
