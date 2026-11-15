import hashlib
import logging

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models.functions import Lower, Trim

from audit.models import record
from config.siconfig import get_config

from .models import ROLE_RANK, Role, User

logger = logging.getLogger("accounts")


class SignInDenied(Exception):
    """Raised when a Google identity is not allowed to use Singularity."""


class UserManagementDenied(Exception):
    """Raised when an admin action on users is refused; the message is safe to show to the admin."""


RoleChangeDenied = UserManagementDenied  # older name, kept so existing imports and except clauses keep working

MAX_EMAIL = 254
MAX_NAME = 150  # Django's first_name / last_name / username length


def normalize_email(raw) -> str:
    """The one canonical form of an email: trimmed and lower-cased. Refuses anything that is not an email."""
    email = raw.strip().lower() if isinstance(raw, str) else ""
    if not email or len(email) > MAX_EMAIL:
        raise UserManagementDenied("enter a valid email address")
    try:
        validate_email(email)
    except ValidationError:
        raise UserManagementDenied("enter a valid email address") from None
    return email


# One message for every refusal, so nothing reveals which accounts exist.
DENIED = "not allowed"


def find_user_by_email(email: str) -> User | None:
    """The user with this (canonical) email, ignoring case and spaces in the stored value; lowest pk if several."""
    return User.objects.annotate(_canon=Lower(Trim("email"))).filter(_canon=email).order_by("pk").first()


def sign_in_from_claims(claims: dict, auth_cfg: dict) -> User:
    """Map verified Google claims to an existing, active User. Sign-in is invite-only: no user is created here."""
    if claims.get("email_verified") is not True:
        raise SignInDenied(DENIED)
    try:
        email = normalize_email(claims.get("email"))
    except UserManagementDenied:
        raise SignInDenied(DENIED) from None
    user = find_user_by_email(email)
    if user is None or not user.is_active:
        raise SignInDenied(DENIED)
    changed = []
    for field, claim in (("first_name", "given_name"), ("last_name", "family_name")):
        value = (claims.get(claim) or "").strip()[:150]
        if value and not getattr(user, field):
            setattr(user, field, value)
            changed.append(field)
    if changed:
        user.save(update_fields=changed)
    record(user, "user.signed_in", target=user.email)
    return user


def _seed_emails(auth_cfg: dict) -> list[str]:
    """seedAdmins as a clean list: lower-cased, de-duplicated, valid emails only (others are skipped with a warning)."""
    raw = auth_cfg.get("seedAdmins")
    if not isinstance(raw, (list, tuple)):
        if raw:
            logger.warning("auth.seedAdmins must be a list of emails; ignoring %r", raw)
        return []
    emails = []
    for item in raw:
        try:
            email = normalize_email(item)
        except UserManagementDenied:
            logger.warning("auth.seedAdmins: skipping invalid entry %r", item)
            continue
        if email not in emails:
            emails.append(email)
    return emails


def ensure_seed_admins(auth_cfg: dict) -> list[User]:
    """Make sure the configured seedAdmins exist as active admins (idempotent, never raises on a bad seed).

    A missing seed email is created as an active admin. If no active admin exists at all, existing ACTIVE seed
    users are promoted to admin (bootstrap); turned-off users are never reactivated, and other users are left alone.
    """
    emails = _seed_emails(auth_cfg)
    has_admin = User.objects.filter(role=Role.ADMIN, is_active=True).exists()
    users = []
    inactive = []
    for email in emails:
        user = find_user_by_email(email)
        if user is None:
            try:
                with transaction.atomic():
                    user = User.objects.create(username=_username_for(email), email=email, role=Role.ADMIN)
                    user.set_unusable_password()
                    user.save(update_fields=["password"])
            except IntegrityError:
                logger.warning("seed admin %s could not be created (username or email already in use)", email)
                continue
            record(user, "user.created", target=email, details={"role": Role.ADMIN.value, "seed": True})
        elif not user.is_active:
            inactive.append(email)
        elif not has_admin and user.role != Role.ADMIN:
            old = user.role
            user.role = Role.ADMIN
            user.save(update_fields=["role"])
            record(user, "user.role_changed", target=email, details={"from": old, "to": Role.ADMIN.value, "seed": True})
        users.append(user)
    if not User.objects.filter(role=Role.ADMIN, is_active=True).exists():
        logger.warning("no active admin exists after seeding; inactive seed users: %s", ", ".join(inactive) or "none")
    return users


def _require_admin(actor: User) -> None:
    if not (actor.is_active and actor.can_admin):
        raise UserManagementDenied("only admins can manage users")


def _username_for(email: str) -> str:
    """username mirrors the email; an email longer than the username column gets a stable hashed name instead."""
    if len(email) <= MAX_NAME:
        return email
    return "u-" + hashlib.sha256(email.encode()).hexdigest()


def _lock_for_change(target: User) -> list[User]:
    """Lock the active admins (pk order), then the target, always in that order so two admins acting on each
    other cannot deadlock. Refreshes the target's role and active flag and returns the locked active admins."""
    admins = list(User.objects.select_for_update().filter(role=Role.ADMIN, is_active=True).order_by("pk"))
    User.objects.select_for_update().filter(pk=target.pk).first()
    target.refresh_from_db(fields=["role", "is_active"])
    return admins


def _guard_last_admin(target: User, admins: list[User]) -> None:
    """Refuse to take admin away from the last active admin (`admins` is the locked active-admin set)."""
    if not any(a.pk != target.pk for a in admins):
        raise UserManagementDenied("cannot remove the last active admin")


def create_user(actor: User, email, role, first_name: str = "", last_name: str = "") -> User:
    """Invite a person: an active user with the given role who can sign in with Google. Admins only."""
    _require_admin(actor)
    email = normalize_email(email)
    if role not in ROLE_RANK:
        raise UserManagementDenied("unknown role")
    domains = get_config()["auth"].get("allowedDomains") or []
    if domains and email.rsplit("@", 1)[1] not in domains:
        raise UserManagementDenied("this email domain is not allowed: " + ", ".join(domains))
    first = (first_name or "").strip()[:MAX_NAME] if isinstance(first_name, str) else ""
    last = (last_name or "").strip()[:MAX_NAME] if isinstance(last_name, str) else ""
    duplicate = UserManagementDenied("a user with this email already exists")
    if find_user_by_email(email) is not None:
        raise duplicate
    try:
        with transaction.atomic():
            user = User(username=_username_for(email), email=email, role=Role(role), first_name=first, last_name=last)
            user.set_unusable_password()
            user.save()
    except IntegrityError:
        raise duplicate from None
    record(actor, "user.created", target=email, details={"role": user.role})
    return user


def set_user_active(actor: User, target: User, active: bool) -> User:
    """Turn a user off (they can no longer sign in or use MCP tokens) or back on. Admins only, never yourself."""
    _require_admin(actor)
    if actor.pk == target.pk:
        raise UserManagementDenied("you cannot turn yourself off or on")
    active = bool(active)
    with transaction.atomic():
        admins = _lock_for_change(target)
        if target.is_active == active:
            return target
        if not active and target.role == Role.ADMIN:
            _guard_last_admin(target, admins)
        target.is_active = active
        target.save(update_fields=["is_active"])
        record(actor, "user.reactivated" if active else "user.deactivated", target=target.email)
    return target


def change_role(actor: User, target: User, new_role: str) -> User:
    """Change someone's role. Admins only (so only an admin grants or removes admin), never your own role."""
    _require_admin(actor)
    if new_role not in ROLE_RANK:
        raise UserManagementDenied("unknown role")
    if actor.pk == target.pk:
        raise UserManagementDenied("you cannot change your own role")
    with transaction.atomic():
        admins = _lock_for_change(target)
        old_role = target.role
        if old_role == new_role:
            return target
        if old_role == Role.ADMIN and target.is_active:
            _guard_last_admin(target, admins)
        target.role = Role(new_role)
        target.save(update_fields=["role"])
        record(actor, "user.role_changed", target=target.email, details={"from": old_role, "to": target.role.value})
    return target
