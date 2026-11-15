import logging

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction

from audit.models import record

from .models import ROLE_RANK, Role, User

logger = logging.getLogger("accounts")


class SignInDenied(Exception):
    """Raised when a Google identity is not allowed to use Singularity."""


class RoleChangeDenied(Exception):
    pass


# One message for every refusal, so nothing reveals which accounts exist.
DENIED = "not allowed"


def sign_in_from_claims(claims: dict, auth_cfg: dict) -> User:
    """Map verified Google claims to an existing, active User. Sign-in is invite-only: no user is created here."""
    email = (claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True:
        raise SignInDenied(DENIED)
    user = User.objects.filter(email__iexact=email).first()
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
        email = item.strip().lower() if isinstance(item, str) else ""
        try:
            validate_email(email)
        except ValidationError:
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
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            try:
                with transaction.atomic():
                    user = User.objects.create(username=email, email=email, role=Role.ADMIN)
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


def change_role(actor: User, target: User, new_role: str) -> User:
    if not actor.can_approve:
        raise RoleChangeDenied("only approvers can change roles")
    if new_role not in ROLE_RANK:
        raise RoleChangeDenied("unknown role")
    old_role = target.role
    if old_role == new_role:
        return target
    if old_role == Role.APPROVER and new_role != Role.APPROVER:
        if User.objects.filter(role=Role.APPROVER, is_active=True).exclude(pk=target.pk).count() == 0:
            raise RoleChangeDenied("cannot remove the last approver")
    target.role = new_role
    target.save(update_fields=["role"])
    record(actor, "user.role_changed", target=target.email, details={"from": old_role, "to": new_role})
    return target
