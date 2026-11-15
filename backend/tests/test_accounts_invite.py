import weakref

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


# ---- fix round 1 ------------------------------------------------------------

def test_bootstrap_does_not_reactivate_inactive_seed_user(caplog):
    from audit.models import AuditLog

    make_user("boss@example.com", Role.APPROVER, active=False)
    make_user("new@example.com", Role.EDITOR)
    with caplog.at_level("WARNING", logger="accounts"):
        ensure_seed_admins({"seedAdmins": ["boss@example.com", "new@example.com"]})
    boss = User.objects.get(email="boss@example.com")
    assert boss.is_active is False and boss.role == Role.APPROVER
    new = User.objects.get(email="new@example.com")
    assert new.role == Role.ADMIN and new.is_active
    entries = list(AuditLog.objects.filter(action="user.role_changed").values_list("target", "details"))
    assert entries == [("new@example.com", {"from": "editor", "to": "admin", "seed": True})]
    # the only seed that could have been an admin is inactive: no active admin besides the promoted one? one exists
    ensure_seed_admins({"seedAdmins": ["boss@example.com", "new@example.com"]})
    assert AuditLog.objects.filter(action="user.role_changed").count() == 1  # nothing changed, nothing recorded


def test_warns_when_no_active_admin_can_be_ensured(caplog):
    make_user("boss@example.com", Role.APPROVER, active=False)
    with caplog.at_level("WARNING", logger="accounts"):
        ensure_seed_admins({"seedAdmins": ["boss@example.com"]})
    assert User.objects.get(email="boss@example.com").is_active is False
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "no active admin" in msg and "boss@example.com" in msg


def test_post_migrate_hook_is_wired_to_accounts_and_default(monkeypatch):
    from django.apps import apps
    from django.db.models.signals import post_migrate

    cfg = apps.get_app_config("accounts")
    cfg.ready()  # conftest disconnects the hook for the test databases; wire it as production does
    try:
        keys = {r[0] for r in post_migrate.receivers}
        assert ("ensure_seed_admins", id(cfg)) in keys
        assert not any(k[0] == "ensure_seed_admins" and k[1] != id(cfg) for k in keys)  # no other sender
        receiver = next(r[1] for r in post_migrate.receivers if r[0] == ("ensure_seed_admins", id(cfg)))
        receiver = receiver() if isinstance(receiver, weakref.ref) else receiver
        monkeypatch.setattr("config.siconfig.get_config", lambda: {"auth": {"seedAdmins": ["seed@example.com"]}})
        receiver(signal=post_migrate, sender=cfg, using="other")
        assert not User.objects.filter(email="seed@example.com").exists()
        receiver(signal=post_migrate, sender=cfg, using="default")
        assert User.objects.get(email="seed@example.com").role == Role.ADMIN
    finally:
        post_migrate.disconnect(dispatch_uid="ensure_seed_admins", sender=cfg)


@pytest.mark.parametrize("flag", ["false", "true", 1, None, "yes"])
def test_email_verified_must_be_boolean_true(flag):
    make_user("a@example.com")
    with pytest.raises(SignInDenied):
        sign_in_from_claims({"email": "a@example.com", "email_verified": flag}, CFG)


def test_seed_admins_config_is_normalised(caplog):
    from accounts.services import ensure_seed_admins as seed

    assert seed({"seedAdmins": "boss@example.com"}) == []  # a scalar is not a list
    assert seed({"seedAdmins": None}) == [] and seed({}) == []
    assert not User.objects.exists()
    with caplog.at_level("WARNING", logger="accounts"):
        got = seed({"seedAdmins": [" Boss@Example.com ", "boss@example.com", "not-an-email", 5, None, "x@y.org"]})
    assert [u.email for u in got] == ["boss@example.com", "x@y.org"]
    assert User.objects.count() == 2
    assert "not-an-email" in " ".join(r.getMessage() for r in caplog.records)


def test_seed_username_collision_does_not_crash(caplog):
    User.objects.create(username="boss@example.com", email="someone-else@example.com", role=Role.VIEWER)
    with caplog.at_level("WARNING", logger="accounts"):
        got = ensure_seed_admins({"seedAdmins": ["boss@example.com", "ok@example.com"]})
    assert [u.email for u in got] == ["ok@example.com"]
    assert User.objects.get(email="ok@example.com").role == Role.ADMIN
    assert not User.objects.filter(email="boss@example.com").exists()
    assert "boss@example.com" in " ".join(r.getMessage() for r in caplog.records)
