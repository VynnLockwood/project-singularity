import hashlib
import logging

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models.functions import Lower, Trim

from audit.models import record
from config.siconfig import get_config

from .models import GRANT_NONE, GRANT_ROLES, ROLE_RANK, Role, SchemaGrant, User

logger = logging.getLogger("accounts")


class SignInDenied(Exception):
    """Raised when a Google identity is not allowed to use Singularity."""


class UserManagementDenied(Exception):
    """Raised when an admin action on users is refused; the message is safe to show to the admin."""


class NotAdmin(UserManagementDenied):
    """The actor is not an (active) admin: the request is forbidden, not merely invalid."""


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


# Reserved example / test domains (RFC 2606, RFC 6761): a seed here is the shipped placeholder or a test value,
# never a real person, so it is never created or promoted.
_PLACEHOLDER_DOMAINS = ("example.com", "example.org", "example.net", "example", "invalid", "test", "localhost")


def _is_placeholder(email: str) -> bool:
    domain = email.rsplit("@", 1)[1]
    return any(domain == d or domain.endswith("." + d) for d in _PLACEHOLDER_DOMAINS)


def _has_real_admin(seeds: list[str]) -> bool:
    """An active admin that counts for bootstrap. Unclaimed seed admins (listed in seedAdmins and never signed in)
    do not count, so a mistyped seed cannot block promoting the right person later."""
    for user in User.objects.filter(role=Role.ADMIN, is_active=True).only("email", "last_login"):
        if user.last_login is not None or (user.email or "").strip().lower() not in seeds:
            return True
    return False


def ensure_seed_admins(auth_cfg: dict) -> list[User]:
    """Make sure the configured seedAdmins exist as active admins (idempotent, never raises on a bad seed).

    Seeds on a reserved placeholder domain (example.com, *.test, localhost, ...) are skipped with a warning.
    A missing seed email is created as an active admin. If no real active admin exists (unclaimed seed admins,
    never signed in, do not count), existing ACTIVE seed users are promoted to admin (bootstrap); turned-off users
    are never reactivated, and other users are left alone.
    """
    emails = []
    for email in _seed_emails(auth_cfg):
        if _is_placeholder(email):
            logger.warning("auth.seedAdmins: skipping %s (reserved placeholder domain; set your real email)", email)
        else:
            emails.append(email)
    has_admin = _has_real_admin(emails)
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


def _close_sockets_on_commit(target: User) -> None:
    """Once the change is committed (never for a dry run that rolls back), close the user's live sockets so they
    reconnect under their new role, or are refused when turned off."""
    from chat import events

    pk = target.pk
    transaction.on_commit(lambda: events.close_user_sockets(pk))


def _require_admin(actor: User) -> None:
    if not (actor.is_active and actor.can_admin):
        raise NotAdmin("only admins can manage users")


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


def create_user(actor: User, email, role, first_name: str = "", last_name: str = "",
                access: list[dict] | None = None, *, via: str = "web") -> User:
    """Invite a person: an active user with the given role who can sign in with Google. Admins only.

    `access` (optional) is the initial list of grants, checked by the same rules as `set_user_access` and written
    in the same transaction: an invalid grant refuses the whole invitation. Without it the user has no grants.
    """
    _require_admin(actor)
    email = normalize_email(email)
    if not isinstance(role, str) or role not in ROLE_RANK:
        raise UserManagementDenied("unknown role")
    domains = get_config()["auth"].get("allowedDomains") or []
    if domains and email.rsplit("@", 1)[1] not in domains:
        raise UserManagementDenied("this email domain is not allowed: " + ", ".join(domains))
    first = (first_name or "").strip()[:MAX_NAME] if isinstance(first_name, str) else ""
    last = (last_name or "").strip()[:MAX_NAME] if isinstance(last_name, str) else ""
    _check_via(via)
    if access is not None:
        _clean_grants(access, email, role)  # refuse bad input before touching the database
    duplicate = UserManagementDenied("a user with this email already exists")
    if find_user_by_email(email) is not None:
        raise duplicate
    with transaction.atomic():
        try:
            with transaction.atomic():
                user = User(username=_username_for(email), email=email, role=Role(role), first_name=first,
                            last_name=last)
                user.set_unusable_password()
                user.save()
        except IntegrityError:
            raise duplicate from None
        record(actor, "user.created", target=email, details={"role": user.role})
        if access:
            _write_grants(actor, user, _clean_grants(access, email, user.role), via)
    return user


# ---- per-database access (grants) --------------------------------------------

ACCESS_CHANNELS = ("web", "mcp")
_GRANT_LEVELS = {r.value for r in GRANT_ROLES} | {GRANT_NONE}


def _check_via(via) -> None:
    if via not in ACCESS_CHANNELS:
        raise UserManagementDenied("via must be web or mcp")


def _grant_key(schema: str) -> tuple:
    """The one canonical grant order: `*` first, then code-point order (never the database's collation)."""
    return (schema != "*", schema)


def _canonical(by_schema: dict[str, str]) -> list[dict]:
    return [{"schema": s, "role": by_schema[s]} for s in sorted(by_schema, key=_grant_key)]


def _stored_map(target: User) -> dict[str, str]:
    return dict(SchemaGrant.objects.filter(user_id=target.pk).values_list("schema", "role"))


def _stored_grants(target: User) -> list[dict]:
    return _canonical(_stored_map(target))


def _short(name: str, limit: int = 64) -> str:
    return name if len(name) <= limit else name[:limit] + "…"


def user_access(target: User) -> dict:
    """The target's stored grants (schema order, `*` first) and their cap, the global role.

    A grant whose database is no longer managed is still listed, flagged `"stale": true`; it has no effect and
    the next `set_user_access` drops it unless it is sent again (which is refused, the database is gone).
    An admin has no grants (they reach every database); rows left from before are never shown as active.
    """
    from schema import introspect

    if target.role == Role.ADMIN:
        return {"grants": [], "cap": str(target.role)}
    grants = _stored_grants(target)
    if grants:
        managed = {m["name"] for m in introspect.managed_schemas()}
        for g in grants:
            if g["schema"] != "*" and g["schema"] not in managed:
                g["stale"] = True
    return {"grants": grants, "cap": str(target.role)}


def access_summary(target: User) -> dict:
    """For the Users page: `all` (admin, or a `*` grant) and the specific grants as `{"name", "role"}`."""
    if target.role == Role.ADMIN:
        return {"all": True, "schemas": []}
    grants = _stored_grants(target)
    return {"all": any(g["schema"] == "*" for g in grants),
            "schemas": [{"name": g["schema"], "role": g["role"]} for g in grants if g["schema"] != "*"]}


def _clean_grants(grants, email: str, cap) -> list[dict]:
    """Check a whole list of grants for a user whose global role is `cap`; returns it sorted by schema.

    Raises UserManagementDenied with a reason the admin can act on. Unknown extra keys (such as the `stale` flag
    that `user_access` returns) are ignored.
    """
    from schema import introspect

    if str(cap) == Role.ADMIN:
        if grants == []:
            return []
        raise UserManagementDenied("admins reach every database; they need no grants")
    if not isinstance(grants, list):
        raise UserManagementDenied("grants must be a list of {schema, role} entries")
    managed = None
    cap_rank = ROLE_RANK[Role(cap)]
    seen: dict[str, str] = {}
    for g in grants:
        if not isinstance(g, dict):
            raise UserManagementDenied("each grant must be an object with a schema and a role")
        schema, role = g.get("schema"), g.get("role")
        if not isinstance(schema, str) or not schema.strip():
            raise UserManagementDenied("each grant needs a schema: a database name, or * for all databases")
        if not isinstance(role, str) or role not in _GRANT_LEVELS:
            raise UserManagementDenied("a grant's role must be viewer, editor, approver or none")
        if schema in seen:
            raise UserManagementDenied(f"database {_short(schema)!r} is listed twice")
        if schema == "*":
            if role == GRANT_NONE:
                raise UserManagementDenied(
                    "All databases (*) cannot be set to none: no entry for All databases already means no access "
                    "there; remove the row instead")
        else:
            if managed is None:
                managed = {m["name"] for m in introspect.managed_schemas()}
            if schema not in managed:
                raise UserManagementDenied(introspect.unknown_schema_message(_short(schema)))
        if role != GRANT_NONE and ROLE_RANK[Role(role)] > cap_rank:
            where = "All databases" if schema == "*" else repr(_short(schema))
            raise UserManagementDenied(
                f"{role} on {where} is above {email}'s global role ({Role(cap).value}); it could never take effect. "
                "Change their role first.")
        seen[schema] = role
    return _canonical(seen)


def _write_grants(actor: User, target: User, after: list[dict], via: str) -> bool:
    """Replace the target's grants with `after` (already checked), inside the caller's transaction.

    Returns False when nothing changes (no write, no audit). Otherwise audits `access.changed` and closes the
    target's live sockets once committed, so the new access applies at once.
    """
    old = _stored_map(target)
    new = {g["schema"]: g["role"] for g in after}
    if old == new:
        return False
    gone = [s for s in old if s not in new]
    if gone:
        SchemaGrant.objects.filter(user_id=target.pk, schema__in=gone).delete()
    for schema, role in new.items():
        if schema in old and old[schema] != role:
            SchemaGrant.objects.filter(user_id=target.pk, schema=schema).update(role=role)
    SchemaGrant.objects.bulk_create(
        [SchemaGrant(user=target, schema=s, role=r) for s, r in new.items() if s not in old])
    record(actor, "access.changed", target=target.email,
           details={"before": _canonical(old), "after": _canonical(new), "via": via})
    _close_sockets_on_commit(target)
    return True


def set_user_access(actor: User, target: User, grants: list[dict], *, via: str = "web") -> dict:
    """Replace all of the target's grants with `grants` (each `{schema, role}`), atomically. Admins only.

    Rules: schema is a managed database or `*`; role is viewer/editor/approver, or none for a specific database
    (a deny under `*`); each database once; never above the target's global role; never for an admin target.
    Returns `user_access(target)`.
    """
    _require_admin(actor)
    _check_via(via)
    with transaction.atomic():
        admins = _lock_for_change(target)  # the cap is the role as committed, not a stale copy
        if not any(a.pk == actor.pk for a in admins):  # the actor lost admin since the request began
            raise NotAdmin("only admins can manage users")
        cleaned = _clean_grants(grants, target.email, target.role)
        if target.role != Role.ADMIN:  # [] for an admin is accepted and changes nothing
            _write_grants(actor, target, cleaned, via)
    return user_access(target)


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
        if not active:
            _close_sockets_on_commit(target)
        record(actor, "user.reactivated" if active else "user.deactivated", target=target.email)
    return target


def change_role(actor: User, target: User, new_role: str) -> User:
    """Change someone's role. Admins only (so only an admin grants or removes admin), never your own role."""
    _require_admin(actor)
    if not isinstance(new_role, str) or new_role not in ROLE_RANK:
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
        _close_sockets_on_commit(target)
        details = {"from": old_role, "to": target.role.value}
        raised = _raise_star_grant(target, old_role, target.role)
        if raised:
            details["access_star_raised"] = raised
        if target.role == Role.ADMIN:
            # admins reach every database without grants; clear them so a later demotion starts with none
            cleared = _stored_grants(target)
            if cleared:
                SchemaGrant.objects.filter(user_id=target.pk).delete()
                details["access_cleared"] = cleared
        record(actor, "user.role_changed", target=target.email, details=details)
    return target


def _raise_star_grant(target: User, old_role, new_role) -> dict | None:
    """On a promotion, a `*` grant that sat exactly at the old global role follows the user up to the new one
    (it tracked the role); a narrower `*` grant, specific grants and every demotion are left alone (the global
    role caps them). Admin is never a grant. Returns `{"from", "to"}` when it raised the grant."""
    if new_role not in GRANT_ROLES or old_role not in GRANT_ROLES:
        return None
    if ROLE_RANK[Role(new_role)] <= ROLE_RANK[Role(old_role)]:
        return None
    if not SchemaGrant.objects.filter(user_id=target.pk, schema="*", role=Role(old_role).value).update(
            role=Role(new_role).value):
        return None
    return {"from": Role(old_role).value, "to": Role(new_role).value}
