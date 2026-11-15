"""Admin user management: adding people, turning them off, role changes (admin only), and role-change approvals."""
import pytest

from accounts import services
from accounts.models import Role, User
from accounts.services import (
    RoleChangeDenied, SignInDenied, UserManagementDenied, change_role, create_user, normalize_email,
    set_user_active, sign_in_from_claims,
)
from approvals import service
from audit.models import AuditLog

pytestmark = pytest.mark.django_db

AUTH = {"allowedDomains": [], "seedAdmins": [], "defaultRole": "viewer"}


@pytest.fixture(autouse=True)
def domains(monkeypatch):
    """allowedDomains for create_user; tests set it with domains[:] = [...]."""
    allowed: list[str] = []
    monkeypatch.setattr(services, "get_config", lambda: {"auth": {**AUTH, "allowedDomains": allowed}})
    return allowed


def make_user(email, role=Role.VIEWER, active=True):
    return User.objects.create(username=email, email=email, role=role, is_active=active)


@pytest.fixture
def admin():
    return make_user("admin@example.com", Role.ADMIN)


# ---- normalize_email ---------------------------------------------------------

def test_normalize_email_trims_lowercases_and_validates():
    assert normalize_email("  Nolan@Gmail.COM ") == "nolan@gmail.com"
    for bad in ("", "   ", "nope", "a@", None, 42, "x" * 250 + "@example.com"):
        with pytest.raises(UserManagementDenied):
            normalize_email(bad)


def test_role_change_denied_is_still_importable_and_caught():
    assert issubclass(RoleChangeDenied, UserManagementDenied) or RoleChangeDenied is UserManagementDenied


# ---- create_user ---------------------------------------------------------------

def test_admin_creates_user_with_role(admin):
    u = create_user(admin, " New.Person@Example.com ", "editor", first_name=" Ada ", last_name="Lovelace")
    u.refresh_from_db()
    assert (u.email, u.role, u.is_active) == ("new.person@example.com", Role.EDITOR, True)
    assert (u.first_name, u.last_name) == ("Ada", "Lovelace")
    assert not u.has_usable_password()
    entry = AuditLog.objects.get(action="user.created")
    assert entry.actor_email == "admin@example.com" and entry.target == "new.person@example.com"
    assert entry.details["role"] == "editor"


def test_create_user_refuses_unknown_role(admin):
    with pytest.raises(UserManagementDenied):
        create_user(admin, "x@example.com", "superuser")
    assert not User.objects.filter(email="x@example.com").exists()


@pytest.mark.parametrize("role", [Role.VIEWER, Role.EDITOR, Role.APPROVER])
def test_non_admin_cannot_create_or_change(role):
    actor = make_user("actor@example.com", role)
    target = make_user("t@example.com")
    with pytest.raises(UserManagementDenied):
        create_user(actor, "new@example.com", "viewer")
    with pytest.raises(UserManagementDenied):
        change_role(actor, target, "editor")
    with pytest.raises(UserManagementDenied):
        set_user_active(actor, target, False)
    target.refresh_from_db()
    assert target.role == Role.VIEWER and target.is_active
    assert not User.objects.filter(email="new@example.com").exists()
    assert not AuditLog.objects.filter(action__startswith="user.").exists()


def test_turned_off_admin_cannot_manage(admin):
    off = make_user("off-admin@example.com", Role.ADMIN, active=False)
    with pytest.raises(UserManagementDenied):
        create_user(off, "new@example.com", "viewer")
    with pytest.raises(UserManagementDenied):
        change_role(off, admin, "viewer")


def test_email_variants_are_one_user(admin):
    create_user(admin, " Nolan@Gmail.com ", "viewer")
    with pytest.raises(UserManagementDenied) as e:
        create_user(admin, "nolan@gmail.com", "editor")
    assert "already" in str(e.value)
    assert User.objects.filter(email__iexact="nolan@gmail.com").count() == 1


def test_email_unique_ignoring_case_in_the_database():
    from django.db import IntegrityError, transaction

    make_user("case@example.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create(username="other", email="CASE@example.com")


def test_domain_guard(admin, domains):
    domains[:] = ["example.com"]
    with pytest.raises(UserManagementDenied):
        create_user(admin, "x@other.com", "viewer")
    assert create_user(admin, "ok@EXAMPLE.com", "viewer").email == "ok@example.com"
    domains[:] = []
    assert create_user(admin, "x@other.com", "viewer").email == "x@other.com"


def test_long_email_gets_a_valid_username(admin):
    email = "a" * 60 + "@" + "b" * 60 + "." + "c" * 60 + ".com"
    assert len(email) > 150
    u = create_user(admin, email, "viewer")
    assert u.email == email and len(u.username) <= 150


# ---- self, last admin, admins managing each other ------------------------------

def test_cannot_change_own_role_or_turn_self_off(admin):
    make_user("admin2@example.com", Role.ADMIN)  # not the last admin: the self rule alone refuses
    with pytest.raises(UserManagementDenied):
        change_role(admin, admin, "viewer")
    with pytest.raises(UserManagementDenied):
        set_user_active(admin, admin, False)
    admin.refresh_from_db()
    assert admin.role == Role.ADMIN and admin.is_active


def test_last_admin_is_protected(admin):
    # the only active admin cannot lose admin: not by themself, not through an approval, not by a stale session
    with pytest.raises(UserManagementDenied):
        change_role(admin, admin, "approver")
    with pytest.raises(UserManagementDenied):
        set_user_active(admin, admin, False)

    editor = make_user("ed@example.com", Role.EDITOR)
    ch = service.propose(editor, [{"op": "set_user_role", "email": "admin@example.com", "role": "viewer"}], source="mcp")
    assert ch.status == "rejected"  # dry run as the only admin: self-change refused

    stale = make_user("stale@example.com", Role.ADMIN)
    User.objects.filter(pk=stale.pk).update(is_active=False)  # turned off elsewhere; this object is out of date
    with pytest.raises(UserManagementDenied) as e:
        change_role(stale, admin, "viewer")
    assert "last" in str(e.value)
    with pytest.raises(UserManagementDenied):
        set_user_active(stale, admin, False)
    admin.refresh_from_db()
    assert admin.role == Role.ADMIN and admin.is_active


def test_admins_manage_each_other(admin):
    other = make_user("admin2@example.com", Role.ADMIN)
    change_role(other, admin, "approver")
    admin.refresh_from_db()
    assert admin.role == Role.APPROVER
    entry = AuditLog.objects.get(action="user.role_changed")
    assert entry.actor_email == "admin2@example.com" and entry.target == "admin@example.com"
    assert entry.details == {"from": "admin", "to": "approver"}
    with pytest.raises(UserManagementDenied):  # now an approver: cannot grant admin back
        change_role(admin, other, "viewer")


def test_only_admin_grants_admin(admin):
    approver = make_user("ap@example.com", Role.APPROVER)
    target = make_user("t@example.com", Role.EDITOR)
    with pytest.raises(UserManagementDenied):
        change_role(approver, target, "admin")
    change_role(admin, target, "admin")
    target.refresh_from_db()
    assert target.role == Role.ADMIN


def test_turned_off_admin_does_not_count_as_another_admin(admin):
    make_user("off@example.com", Role.ADMIN, active=False)
    stale = make_user("stale@example.com", Role.ADMIN)
    User.objects.filter(pk=stale.pk).update(is_active=False)
    with pytest.raises(UserManagementDenied):
        change_role(stale, admin, "editor")


# ---- turning people off and on ------------------------------------------------

def test_turned_off_user_cannot_sign_in(admin):
    u = make_user("u@example.com", Role.EDITOR)
    claims = {"email": "u@example.com", "email_verified": True}
    set_user_active(admin, u, False)
    u.refresh_from_db()
    assert not u.is_active
    with pytest.raises(SignInDenied):
        sign_in_from_claims(claims, AUTH)
    off = AuditLog.objects.get(action="user.deactivated")
    assert off.actor_email == "admin@example.com" and off.target == "u@example.com"

    set_user_active(admin, u, True)
    assert sign_in_from_claims(claims, AUTH).pk == u.pk
    on = AuditLog.objects.get(action="user.reactivated")
    assert on.actor_email == "admin@example.com" and on.target == "u@example.com"


def test_setting_the_same_state_is_a_quiet_no_op(admin):
    u = make_user("u@example.com")
    set_user_active(admin, u, True)
    assert not AuditLog.objects.filter(action__in=["user.deactivated", "user.reactivated"]).exists()


# ---- role changes through the approval queue ------------------------------------

def test_approver_cannot_decide_role_change(admin):
    editor = make_user("ed@example.com", Role.EDITOR)
    approver = make_user("ap@example.com", Role.APPROVER)
    make_user("v@example.com")
    ch = service.propose(editor, [{"op": "set_user_role", "email": "v@example.com", "role": "editor"}], source="mcp")
    assert ch.status == "pending"
    assert not service.can_decide(approver, ch) and service.can_decide(admin, ch)
    with pytest.raises(service.ChangeError) as e:
        service.approve(approver, ch.pk)
    assert e.value.code == "forbidden"
    assert service.approve(admin, ch.pk).status == "applied"
    assert User.objects.get(email="v@example.com").role == Role.EDITOR

    plain = service.propose(editor, [{"op": "set_setting", "key": "search.topK", "value": 7}], source="mcp")
    assert service.can_decide(approver, plain)
    assert service.approve(approver, plain.pk).status == "applied"


def test_mixed_config_change_with_a_role_op_needs_an_admin(admin):
    editor = make_user("ed@example.com", Role.EDITOR)
    approver = make_user("ap@example.com", Role.APPROVER)
    ch = service.propose(editor, [{"op": "set_setting", "key": "search.topK", "value": 7},
                                  {"op": "set_user_role", "email": "ed@example.com", "role": "viewer"}], source="mcp")
    assert ch.status == "pending" and not service.can_decide(approver, ch) and service.can_decide(admin, ch)


def test_approver_proposing_a_role_change_is_dry_run_as_an_admin(admin):
    approver = make_user("ap@example.com", Role.APPROVER)
    make_user("v@example.com")
    ch = service.propose(approver, [{"op": "set_user_role", "email": "v@example.com", "role": "editor"}], source="mcp")
    assert ch.status == "pending" and ch.diff == ["~ role v@example.com: viewer → editor"]
    assert User.objects.get(email="v@example.com").role == Role.VIEWER


def test_role_ops_need_an_admin_to_exist():
    editor = make_user("ed@example.com", Role.EDITOR)
    make_user("ap@example.com", Role.APPROVER)
    with pytest.raises(service.ChangeError) as e:
        service.propose(editor, [{"op": "set_user_role", "email": "ed@example.com", "role": "viewer"}], source="mcp")
    assert e.value.code == "invalid"


# ---- _dry_run_actor -----------------------------------------------------------------

ROLE_OPS = [{"op": "set_user_role", "email": "x@example.com", "role": "editor"}]
PLAIN_OPS = [{"op": "set_setting", "key": "search.topK", "value": 7}]


def test_dry_run_actor_for_role_ops_picks_an_active_admin():
    editor = make_user("ed@example.com", Role.EDITOR)
    approver = make_user("ap@example.com", Role.APPROVER)
    make_user("off-admin@example.com", Role.ADMIN, active=False)
    adm = make_user("adm@example.com", Role.ADMIN)
    assert service._dry_run_actor(editor, ROLE_OPS) == adm
    assert service._dry_run_actor(approver, ROLE_OPS) == adm
    assert service._dry_run_actor(adm, ROLE_OPS) == adm


def test_dry_run_actor_for_role_ops_prefers_an_admin_who_is_not_the_target():
    editor = make_user("ed@example.com", Role.EDITOR)
    first = make_user("first@example.com", Role.ADMIN)
    second = make_user("second@example.com", Role.ADMIN)
    ops = [{"op": "set_user_role", "email": "FIRST@example.com", "role": "editor"}]
    assert service._dry_run_actor(editor, ops) == second
    assert service._dry_run_actor(editor, ROLE_OPS) == first


def test_dry_run_actor_for_other_ops_picks_any_active_approver_or_admin():
    editor = make_user("ed@example.com", Role.EDITOR)
    make_user("off-ap@example.com", Role.APPROVER, active=False)
    with pytest.raises(service.ChangeError):
        service._dry_run_actor(editor, PLAIN_OPS)
    adm = make_user("adm@example.com", Role.ADMIN)
    assert service._dry_run_actor(editor, PLAIN_OPS) == adm  # an admin is an approver too
    approver = make_user("ap@example.com", Role.APPROVER)
    assert service._dry_run_actor(approver, PLAIN_OPS) == approver
    assert service._dry_run_actor(editor, PLAIN_OPS).can_approve


# ---- migration 0006: canonical emails ------------------------------------------------

def _migration():
    import importlib

    return importlib.import_module("accounts.migrations.0006_email_ci_unique")


def test_migration_lowercases_existing_emails():
    from django.apps import apps

    u = make_user(" Mixed@Example.COM")
    _migration().normalize_emails(apps, None)
    u.refresh_from_db()
    assert u.email == "mixed@example.com"


def test_migration_stops_on_clashing_emails_and_changes_nothing():
    class Rows:
        def values_list(self, *fields):
            return [(1, "Dup@Example.com"), (2, "dup@example.com "), (3, "solo@example.com")]

        def all(self):
            raise AssertionError("nothing may be written when emails clash")

    class FakeUser:
        objects = Rows()

    class FakeApps:
        def get_model(self, app, name):
            return FakeUser

    with pytest.raises(RuntimeError) as e:
        _migration().normalize_emails(FakeApps(), None)
    assert "dup@example.com: user ids [1, 2]" in str(e.value) and "solo" not in str(e.value)
