from audit.models import record

from .models import ROLE_RANK, Role, User


class SignInDenied(Exception):
    """Raised when a Google identity is not allowed to use Singularity."""


class RoleChangeDenied(Exception):
    pass


def sign_in_from_claims(claims: dict, auth_cfg: dict) -> User:
    """Map verified Google claims to a User, creating one on first sign-in."""
    email = (claims.get("email") or "").strip().lower()
    if not email or not claims.get("email_verified"):
        raise SignInDenied("email not verified")

    domain = email.rsplit("@", 1)[-1]
    allowed = auth_cfg["allowedDomains"]
    if not allowed or domain not in allowed:
        raise SignInDenied("domain not allowed")

    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        role = Role.APPROVER if email in auth_cfg["seedAdmins"] else Role(auth_cfg["defaultRole"])
        user = User.objects.create(
            username=email,
            email=email,
            first_name=(claims.get("given_name") or "")[:150],
            last_name=(claims.get("family_name") or "")[:150],
            role=role,
        )
        user.set_unusable_password()
        user.save(update_fields=["password"])
        record(user, "user.created", target=email, details={"role": role.value})
    if not user.is_active:
        raise SignInDenied("account disabled")
    record(user, "user.signed_in", target=email)
    return user


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
