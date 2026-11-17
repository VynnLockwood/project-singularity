import re

from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Append-only record of who did what (spec section 2, audit)."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    actor_email = models.EmailField(blank=True)  # kept even if the user row is removed
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=255, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["action", "-created_at"])]


def record(actor, action: str, target: str = "", details: dict | None = None) -> AuditLog:
    return AuditLog.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        actor_email=getattr(actor, "email", "") or "",
        action=action,
        target=target,
        details=details or {},
    )


# entries whose details name their database (details["schema"]); others are worked out in `_databases`
_NAMED = {"schema.export", "table.export_csv", "duplicates.scanned", "change.proposal_failed"}
_BACKUP_RE = re.compile(r"^(?P<schema>[a-z][a-z0-9_]*)-\d{8}T\d{6}Z-.+\.dump$")
_ID_RE = re.compile(r"^#(\d+)$")
UNKNOWN = None   # the database of an entry that should name one but cannot be worked out: admins only
BATCH = 200


def _id(target: str) -> int | None:
    m = _ID_RE.match(target or "")
    return int(m.group(1)) if m else None


def _grant_names(grants) -> set[str]:
    return {g["schema"] for g in grants if isinstance(g, dict) and isinstance(g.get("schema"), str)} \
        if isinstance(grants, list) else set()


def _databases(a: AuditLog, changes: dict, groups: dict) -> set[str] | None:
    """The databases an entry is about: an empty set for one about none (settings, users, sign-ins, config
    changes), or UNKNOWN when it should name one but it cannot be told (fail closed)."""
    from accounts import access

    d = a.details if isinstance(a.details, dict) else {}
    named = d.get("schema")
    if isinstance(named, str) and named:
        return {named}
    act = a.action
    if act in _NAMED:
        return UNKNOWN
    if act.startswith("load."):
        db = (a.target or "").split(".", 1)[0]
        return {db} if "." in (a.target or "") and db else UNKNOWN
    if act == "backup.created":
        m = _BACKUP_RE.match(a.target or "")
        return {m["schema"]} if m else UNKNOWN
    if act in ("duplicates.dismissed", "duplicates.reopened"):
        g = groups.get(_id(a.target))
        return {g} if g else UNKNOWN
    if act.startswith("change."):
        ch = changes.get(_id(a.target))
        if ch is None:
            return UNKNOWN
        kind, schema, ops = ch
        if kind == "config" or access.is_config_ops(ops):
            return set()           # decided by global role, like today
        return access.affected_schemas(ops, schema or "master")   # database ops are filed under master
    if act == "access.changed":
        return (_grant_names(d.get("before")) | _grant_names(d.get("after"))) - {access.ALL}
    if act == "user.role_changed":
        return (_grant_names(d.get("access_cleared")) | _grant_names(d.get("access_star_raised"))) - {access.ALL}
    return set()


def _related(batch: list[AuditLog]) -> tuple[dict, dict]:
    """The changes and duplicate groups a batch of entries points at by `#id`, read once per batch."""
    from approvals.models import Change
    from duplicates.models import DuplicateGroup

    change_ids = {i for a in batch if a.action.startswith("change.") and (i := _id(a.target)) is not None}
    group_ids = {i for a in batch if a.action.startswith("duplicates.") and (i := _id(a.target)) is not None}
    changes = {pk: (kind, schema, ops) for pk, kind, schema, ops in
               Change.objects.filter(pk__in=change_ids).values_list("pk", "kind", "schema", "operations")}
    groups = dict(DuplicateGroup.objects.filter(pk__in=group_ids).values_list("pk", "schema"))
    return changes, groups


def _sees(viewer):
    """A predicate: may `viewer` see an entry about these databases? Approver on every one of them; an entry
    about none is shown as today; one whose database cannot be told only to admins."""
    from accounts import access
    from accounts.models import Role

    if getattr(viewer, "is_active", False) and getattr(viewer, "role", None) == Role.ADMIN:
        return lambda dbs: True
    memo: dict[str, bool] = {}

    def ok(dbs) -> bool:
        if dbs is UNKNOWN:
            return False
        for db in dbs:
            if db not in memo:
                memo[db] = access.can(viewer, db, Role.APPROVER)
            if not memo[db]:
                return False
        return True

    return ok


def entries(*, action: str | None = None, actor: str | None = None, limit: int = 100,
            viewer=None) -> tuple[list[AuditLog], bool]:
    """Audit entries, newest first, and whether more exist beyond `limit`. `actor` is an email, any letter case.

    With `viewer`, an entry about a database is kept only when the viewer is approver on it (on all of them for
    an entry about several); entries about none behave as today, and one that should name a database but
    cannot be told is for admins only. The filter runs before the limit, so `limit` counts shown entries and
    the "more" flag says whether more shown entries exist. Without `viewer` nothing is filtered.
    """
    qs = AuditLog.objects.all()
    if action:
        qs = qs.filter(action=action)
    if actor:
        qs = qs.filter(actor_email__iexact=actor)
    if viewer is None:
        rows = list(qs[: limit + 1])
        return rows[:limit], len(rows) > limit
    sees = _sees(viewer)
    kept: list[AuditLog] = []
    start = 0
    while len(kept) <= limit:
        batch = list(qs[start:start + BATCH])   # the order is fixed (-created_at, -id), so pages do not overlap
        if not batch:
            break
        start += len(batch)
        changes, groups = _related(batch)
        kept += [a for a in batch if sees(_databases(a, changes, groups))]
    return kept[:limit], len(kept) > limit


def as_dict(a: AuditLog) -> dict:
    return {"id": a.pk, "at": a.created_at.isoformat(), "actor": a.actor_email, "action": a.action,
            "target": a.target, "details": a.details}
