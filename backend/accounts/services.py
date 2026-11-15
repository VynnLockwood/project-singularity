from audit.models import record

from .models import ROLE_RANK, Role, User


class SignInDenied(Exception):
    """Raised when a Google identity is not allowed to use Singularity."""


class RoleChangeDenied(Exception):
    pass


# One message for every refusal, so nothing reveals which accounts exist.
DENIED = "not allowed"


def sign_in_from_claims(claims: dict, auth_cfg: dict) -> User:
    """Map verified Google claims to an existing, active User. Sign-in is invite-only: no user is created here."""
    email = (claims.get("email") or "").strip().lower()
    if not email or not claims.get("email_verified"):
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


def ensure_seed_admins(auth_cfg: dict) -> list[User]:
    """Make sure the configured seedAdmins exist as active admins (idempotent).

    A missing seed email is created as an active admin. If no active admin exists at all, existing seed users
    are promoted to admin (bootstrap); otherwise existing users are left exactly as they are.
    """
    emails = []
    for raw in auth_cfg.get("seedAdmins") or []:
        email = (raw or "").strip().lower()
        if email and email not in emails:
            emails.append(email)
    has_admin = User.objects.filter(role=Role.ADMIN, is_active=True).exists()
    users = []
    for email in emails:
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            user = User.objects.create(username=email, email=email, role=Role.ADMIN)
            user.set_unusable_password()
            user.save(update_fields=["password"])
            record(user, "user.created", target=email, details={"role": Role.ADMIN.value, "seed": True})
        elif not has_admin and (user.role != Role.ADMIN or not user.is_active):
            old = user.role
            user.role = Role.ADMIN
            user.is_active = True
            user.save(update_fields=["role", "is_active"])
            record(user, "user.role_changed", target=email, details={"from": old, "to": Role.ADMIN.value, "seed": True})
        users.append(user)
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
