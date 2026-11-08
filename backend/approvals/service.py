"""Change lifecycle and the approval rules (spec section 2).

  proposed -> checked -> pending -> approved -> applied
                  |          |                     |
              rejected   rejected               failed (rolled back)
                             stale (schema or rules moved; needs a re-check)

Who may decide:
  schema change : approvers only (editors' proposals wait in the queue)
  script load   : approvers only (whoever ran it must not be the one who applies it)
  data change   : its proposer (editor+) or any approver
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from audit.models import record
from config.siconfig import get_config
from gate import check, operations
from schema import executor, introspect
from schema.backup import BackupError, backup_master
from settings_app import services as rule_service

from . import config_ops
from .models import Change


class ChangeError(Exception):
    """code: forbidden | not_found | bad_state | invalid"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _kind(raw) -> str:
    if isinstance(raw, list) and any(isinstance(o, dict) and o.get("op") in operations.SCHEMA_OPS for o in raw):
        return "schema"
    return "data"


def _affected(ops) -> list[str]:
    try:
        return operations.affected_tables(ops)
    except (KeyError, TypeError):
        return []


def can_decide(user, change: Change) -> bool:
    if change.source == "script" or change.kind in ("schema", "config"):
        # structure, rules, settings, roles and every script load: approvers only
        return user.can_approve
    return user.can_approve or (user.can_edit and user.pk == change.requester_id)


def _apply_check(change: Change, res, *, ops_for_tables) -> None:
    change.operations = res.operations
    change.check_result = res.as_dict()
    change.generated_sql = res.sql
    change.diff = res.diff
    change.affected_tables = _affected(ops_for_tables)
    change.fingerprint = introspect.fingerprint(change.affected_tables) if change.affected_tables else ""
    change.rules_hash = rule_service.rules_hash()
    change.status = Change.Status.REJECTED if res.blocked else Change.Status.PENDING
    if res.blocked:
        change.decision_note = "blocked by the rules gate"


def _dry_run_actor(user):
    """Who the dry run pretends to be: the services insist on an approver, and the real approver re-checks later."""
    from accounts.models import Role, User

    if user.can_approve:
        return user
    approver = User.objects.filter(role=Role.APPROVER, is_active=True).first()
    if approver is None:
        raise ChangeError("invalid", "there is no approver to review a configuration change")
    return approver


def propose_config(user, raw_ops, *, source: str, prompt: str = "", conversation_id: int | None = None,
                   reverts: Change | None = None) -> Change:
    """A settings / rules / roles change: checked now, applied only when an approver approves it."""
    if not user.can_edit:
        raise ChangeError("forbidden", "your role cannot propose changes")
    res = config_ops.run(raw_ops, _dry_run_actor(user), dry_run=True)
    change = Change(
        requester=user, source=source, prompt=prompt, conversation_id=conversation_id, kind="config",
        original_operations=raw_ops if isinstance(raw_ops, list) else [], operations=raw_ops if isinstance(raw_ops, list) else [],
        diff=res.diff, check_result={"errors": res.errors, "violations": [], "autoAdded": []}, generated_sql=[],
        affected_tables=[], reverts=reverts,
        status=Change.Status.REJECTED if res.errors else Change.Status.PENDING,
    )
    if res.errors:
        change.decision_note = "blocked: the configuration change is not valid"
    change.save()
    record(user, "change.proposed", target=f"#{change.pk}", details={"source": source, "kind": "config", "status": change.status})
    return change


def propose(user, raw_ops, *, source: str, prompt: str = "", conversation_id: int | None = None,
            reverts: Change | None = None) -> Change:
    if config_ops.has_config(raw_ops):
        if not config_ops.is_config(raw_ops):
            raise ChangeError("invalid", "configuration operations (settings, rules, roles) cannot be mixed with schema or data operations; propose them separately")
        return propose_config(user, raw_ops, source=source, prompt=prompt, conversation_id=conversation_id, reverts=reverts)
    if not user.can_edit:
        raise ChangeError("forbidden", "your role cannot propose changes")
    limits = get_config()["limits"]
    is_revert = reverts is not None
    max_ops = max(limits["maxOpsPerProposal"], len(raw_ops)) if is_revert and isinstance(raw_ops, list) \
        else limits["maxOpsPerProposal"]

    res = check.run(
        raw_ops, introspect.snapshot(), rule_service.active_rules(),
        actor=user.email, max_ops=max_ops, skip_rules=is_revert,
        allow_bulk=source == "script",
    )
    change = Change(
        requester=user, source=source, prompt=prompt, conversation_id=conversation_id,
        kind=_kind(raw_ops), original_operations=raw_ops if isinstance(raw_ops, list) else [], reverts=reverts,
    )
    _apply_check(change, res, ops_for_tables=res.operations)
    change.save()
    record(user, "change.proposed", target=f"#{change.pk}",
           details={"source": source, "kind": change.kind, "status": change.status})
    if change.status == Change.Status.REJECTED:
        record(user, "change.rejected_by_gate", target=f"#{change.pk}", details=change.check_result)
    return change


def _script_row_source(change: Change):
    """Row provider for a script-sourced change; None for every other source (lazy import: no cycle)."""
    if change.source != "script":
        return None
    from scripts.models import Script
    from scripts.rowsource import make_row_source
    script = Script.objects.filter(change_id=change.pk).first()
    if script is None:
        raise executor.ApplyError(f"change #{change.pk} is script-sourced but has no script")
    return make_row_source(script)


def _get_for_update(change_id: int) -> Change:
    change = Change.objects.select_for_update().filter(pk=change_id).first()
    if change is None:
        raise ChangeError("not_found", f"change #{change_id} does not exist")
    return change


def approve(user, change_id: int) -> Change:
    """Approve and apply. Returns the change: check `.status` (applied | failed | stale)."""
    cfg = get_config()
    with transaction.atomic():
        change = _get_for_update(change_id)
        if change.status != Change.Status.PENDING:
            raise ChangeError("bad_state", f"change #{change.pk} is {change.status}, not pending")
        if not can_decide(user, change):
            raise ChangeError("forbidden", "you cannot approve this change")

        if change.kind == "config":
            return _apply_config(change, user)

        is_revert = change.source == "revert"
        moved = (
            (change.affected_tables and introspect.fingerprint(change.affected_tables) != change.fingerprint)
            or (not is_revert and rule_service.rules_hash() != change.rules_hash)
        )
        state = introspect.snapshot()
        recheck = check.run(
            change.operations, state, rule_service.active_rules(), actor=change.requester.email,
            max_ops=max(len(change.operations), 1), skip_rules=True,
            allow_bulk=change.source == "script",
        )
        if moved or recheck.errors:
            change.status = Change.Status.STALE
            change.error = "; ".join(recheck.errors) or "the schema or the rules changed since this was checked"
            change.save()
            record(user, "change.stale", target=f"#{change.pk}", details={"error": change.error})
            return change

        change.decided_by, change.decided_at = user, timezone.now()
        record(user, "change.approved", target=f"#{change.pk}")

        if change.kind == "schema":
            try:
                path = backup_master(f"change-{change.pk}", cfg["backups"]["retentionDays"])
            except BackupError as exc:
                return _fail(change, user, f"backup failed, nothing was changed: {exc}")
            record(user, "backup.created", target=path.name)

        # a revert or a script load may touch as many rows as the script inserted, so they use the script cap
        max_rows = cfg["limits"]["maxRowsPerDataOp"]
        if is_revert or change.source == "script":
            max_rows = max(max_rows, cfg["limits"]["maxScriptRows"])
        try:
            result = executor.execute(
                change.operations, state, change.requester.email, max_rows=max_rows,
                rules=rule_service.active_rules(),
                row_source=_script_row_source(change),
            )
        except executor.ApplyError as exc:
            return _fail(change, user, str(exc))

        change.status = Change.Status.APPLIED
        change.applied_at = timezone.now()
        change.reverse_operations = result.reverse_ops
        change.error = ""
        change.save()
        record(user, "change.applied", target=f"#{change.pk}",
               details={"kind": change.kind, "tables": change.affected_tables, "reversible": result.reverse_ops is not None})
        return change


def _apply_config(change: Change, user) -> Change:
    res = config_ops.run(change.operations, user, dry_run=False)
    if res.errors:
        return _fail(change, user, "; ".join(res.errors) + " (nothing was changed)")
    change.decided_by, change.decided_at = user, timezone.now()
    change.status = Change.Status.APPLIED
    change.applied_at = timezone.now()
    change.reverse_operations = res.reverse
    change.diff = res.diff  # what actually happened, in case the values moved since the proposal
    change.error = ""
    change.save()
    record(user, "change.approved", target=f"#{change.pk}")
    record(user, "change.applied", target=f"#{change.pk}", details={"kind": "config", "ops": len(change.operations), "reversible": True})
    return change


def _fail(change: Change, user, message: str) -> Change:
    change.status = Change.Status.FAILED
    change.error = message
    change.save()
    record(user, "change.failed", target=f"#{change.pk}", details={"error": message})
    return change


def reject(user, change_id: int, note: str = "") -> Change:
    with transaction.atomic():
        change = _get_for_update(change_id)
        if change.status not in (Change.Status.PENDING, Change.Status.STALE):
            raise ChangeError("bad_state", f"change #{change.pk} is {change.status}")
        if not (user.can_approve or (user.can_edit and user.pk == change.requester_id)):
            raise ChangeError("forbidden", "you cannot reject this change")
        change.status = Change.Status.REJECTED
        change.decided_by, change.decided_at = user, timezone.now()
        change.decision_note = note
        change.save()
        record(user, "change.rejected", target=f"#{change.pk}", details={"note": note})
        return change


def recheck(user, change_id: int) -> Change:
    """Run the gate again on the original operations (after a stale result or a rules change)."""
    with transaction.atomic():
        change = _get_for_update(change_id)
        if change.status not in (Change.Status.STALE, Change.Status.PENDING, Change.Status.REJECTED):
            raise ChangeError("bad_state", f"change #{change.pk} is {change.status}")
        if not (user.can_approve or user.pk == change.requester_id):
            raise ChangeError("forbidden", "only the requester or an approver can re-check")
        if change.status == Change.Status.REJECTED and change.decided_by_id is not None:
            raise ChangeError("bad_state", "a change declined by a person cannot be re-checked; propose it again")
        if change.kind == "config":
            res = config_ops.run(change.original_operations, _dry_run_actor(user), dry_run=True)
            change.diff = res.diff
            change.check_result = {"errors": res.errors, "violations": [], "autoAdded": []}
            change.status = Change.Status.REJECTED if res.errors else Change.Status.PENDING
            change.decision_note = "blocked: the configuration change is not valid" if res.errors else ""
            change.error = ""
            change.save()
            record(user, "change.rechecked", target=f"#{change.pk}", details={"status": change.status})
            return change
        is_revert = change.source == "revert"
        res = check.run(
            change.original_operations, introspect.snapshot(), rule_service.active_rules(),
            actor=change.requester.email, max_ops=max(len(change.original_operations), 1), skip_rules=is_revert,
        )
        _apply_check(change, res, ops_for_tables=res.operations)
        change.error = ""
        if change.status == Change.Status.PENDING:
            change.decision_note = ""
        change.save()
        record(user, "change.rechecked", target=f"#{change.pk}", details={"status": change.status})
        return change


def revert(user, change_id: int) -> Change:
    """Undo an applied change by applying its stored reverse operations (approvers only)."""
    if not user.can_approve:
        raise ChangeError("forbidden", "only approvers can revert changes")
    orig = Change.objects.filter(pk=change_id).first()
    if orig is None:
        raise ChangeError("not_found", f"change #{change_id} does not exist")
    if orig.status != Change.Status.APPLIED:
        raise ChangeError("bad_state", f"change #{orig.pk} is {orig.status}, only applied changes can be reverted")
    if orig.reverse_operations is None:
        raise ChangeError("bad_state", f"change #{orig.pk} cannot be undone automatically (it dropped something)")
    if Change.objects.filter(reverts=orig).exclude(status__in=[Change.Status.REJECTED, Change.Status.FAILED]).exists():
        raise ChangeError("bad_state", f"change #{orig.pk} was already reverted or has a revert pending")

    if orig.kind == "config":
        rev = propose_config(user, orig.reverse_operations, source="revert", prompt=f"Revert change #{orig.pk}", reverts=orig)
    else:
        rev = propose(user, orig.reverse_operations, source="revert", prompt=f"Revert change #{orig.pk}", reverts=orig)
    if rev.status != Change.Status.PENDING:
        return rev
    rev = approve(user, rev.pk)  # an approver may approve their own change
    if rev.status == Change.Status.APPLIED:
        record(user, "change.reverted", target=f"#{orig.pk}", details={"by": f"#{rev.pk}"})
    return rev


def pending_for(user):
    """Changes this user could decide right now (the review queue)."""
    qs = Change.objects.filter(status=Change.Status.PENDING)
    if user.can_approve:
        return qs
    if user.can_edit:
        return qs.filter(kind="data", requester=user)
    return qs.none()


def serialize(change: Change, *, detail: bool = True) -> dict:
    out = {
        "id": change.pk,
        "status": change.status,
        "kind": change.kind,
        "source": change.source,
        "requester": change.requester.email,
        "prompt": change.prompt,
        "createdAt": change.created_at.isoformat(),
        "decidedBy": change.decided_by.email if change.decided_by_id else None,
        "decidedAt": change.decided_at.isoformat() if change.decided_at else None,
        "appliedAt": change.applied_at.isoformat() if change.applied_at else None,
        "error": change.error,
        "decisionNote": change.decision_note,
        "reversible": change.reverse_operations is not None if change.status == "applied" else None,
        "reverts": change.reverts_id,
        "tables": change.affected_tables,
        "diff": change.diff,
        "check": change.check_result,
    }
    if detail:
        out["operations"] = change.operations
        out["sql"] = change.generated_sql
    return out
