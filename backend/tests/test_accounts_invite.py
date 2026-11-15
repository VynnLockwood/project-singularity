import pytest
from django.test import Client

from accounts import google
from accounts.models import Role, User
from accounts.services import SignInDenied, ensure_seed_admins, sign_in_from_claims

pytestmark = pytest.mark.django_db

CFG = {"allowedDomains": ["example.com"], "seedAdmins": ["boss@example.com", "new@example.com"], "defaultRole": "viewer"}


def claims(email, verified=True, **extra):
    return {"email": email, "email_verified": verified, **extra}


def make_user(email, role=Role.VIEWER, active=True):
    return User.objects.create(username=email, email=email, role=role, is_active=active)


def test_unknown_email_denied_same_as_off_user():
    make_user("off@example.com", active=False)
    with pytest.raises(SignInDenied) as unknown:
        sign_in_from_claims(claims("nobody@example.com"), CFG)
    with pytest.raises(SignInDenied) as off:
        sign_in_from_claims(claims("off@example.com"), CFG)
    assert str(unknown.value) == str(off.value)
    assert not User.objects.filter(email="nobody@example.com").exists()
    assert User.objects.count() == 1


def test_existing_user_signs_in_case_insensitively():
    u = make_user("ann@other.org", Role.EDITOR)
    got = sign_in_from_claims(claims("  Ann@Other.ORG ", given_name="Ann", family_name="Lee"), CFG)
    assert got.pk == u.pk and got.role == Role.EDITOR
    assert (got.first_name, got.last_name) == ("Ann", "Lee")
    u.first_name = "Keep"
    u.save()
    assert sign_in_from_claims(claims("ann@other.org", given_name="Other"), CFG).first_name == "Keep"


def test_unverified_email_denied():
    make_user("a@example.com")
    with pytest.raises(SignInDenied) as unverified:
        sign_in_from_claims(claims("a@example.com", verified=False), CFG)
    with pytest.raises(SignInDenied) as unknown:
        sign_in_from_claims(claims("zzz@example.com"), CFG)
    assert str(unverified.value) == str(unknown.value)


def test_oauth_unknown_email_cannot_connect(monkeypatch):
    from tests.test_oauth import REDIRECT, authorize_url, pkce, register

    cid = register(Client()).json()["client_id"]
    _, challenge = pkce()
    c = Client()
    url = authorize_url(cid, challenge)
    assert c.get(url)["Location"] == "/api/auth/google/login"
    monkeypatch.setattr("accounts.views.get_config", lambda: {"auth": CFG})
    c.get("/api/auth/google/login")
    state = c.session["google_oauth_state"]
    monkeypatch.setattr(google, "fetch_claims", lambda code, uri: claims("stranger@example.com"))
    resp = c.get("/api/auth/google/callback", {"code": "abc", "state": state})
    assert resp.status_code == 403 and resp.json() == {"error": "not allowed"}
    again = c.get(url)  # still signed out: sent back to sign in, never to consent
    assert again.status_code == 302 and again["Location"] == "/api/auth/google/login"
    assert not User.objects.filter(email="stranger@example.com").exists()


def test_seed_admin_bootstrap():
    make_user("boss@example.com", Role.APPROVER)
    made = ensure_seed_admins(CFG)
    assert {u.email for u in made} == {"boss@example.com", "new@example.com"}
    assert User.objects.get(email="boss@example.com").role == Role.ADMIN
    new = User.objects.get(email="new@example.com")
    assert new.role == Role.ADMIN and new.is_active and not new.has_usable_password()
    snapshot = list(User.objects.order_by("email").values_list("email", "role", "is_active"))
    ensure_seed_admins(CFG)  # idempotent
    assert list(User.objects.order_by("email").values_list("email", "role", "is_active")) == snapshot


def test_seed_admin_with_active_admin_present_leaves_existing_alone():
    make_user("root@example.com", Role.ADMIN)
    make_user("boss@example.com", Role.VIEWER)
    ensure_seed_admins(CFG)
    assert User.objects.get(email="boss@example.com").role == Role.VIEWER
    assert User.objects.get(email="new@example.com").role == Role.ADMIN  # missing ones are still created
    count = User.objects.count()
    ensure_seed_admins(CFG)
    assert User.objects.count() == count


def test_existing_users_stay_active_after_bootstrap():
    make_user("a@example.com", Role.VIEWER)
    make_user("b@example.com", Role.EDITOR)
    make_user("boss@example.com", Role.APPROVER)
    ensure_seed_admins(CFG)
    roles = dict(User.objects.values_list("email", "role"))
    assert roles["a@example.com"] == Role.VIEWER and roles["b@example.com"] == Role.EDITOR
    assert User.objects.filter(is_active=False).count() == 0
