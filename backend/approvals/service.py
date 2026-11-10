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

import hashlib
import json

import psycopg
from django.db import transaction
from django.utils import timezone

from audit.models import record
from config.siconfig import get_config
from gate import check, operations
from schema import executor, introspect
from schema.backup import BackupError, backup_schema
from settings_app import services as rule_service

from . import config_ops, rawsql
from .models import Change


class ChangeError(Exception):
    """code: forbidden | not_found | bad_state | invalid"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _kind(raw) -> str:
    if isinstance(raw, list) and all(isinstance(o, dict) and "op" in o for o in raw):
        return operations.kind_of(raw)
    return "data"


def _affected(ops) -> list[str]:
    try:
        return operations.affected_tables(ops)
    except (KeyError, TypeError):
        return []


def _is_database(ops) -> bool:
    return (isinstance(ops, list) and bool(ops) and all(isinstance(o, dict) for o in ops)
            and any(o.get("op") in operations.DATABASE_OPS for o in ops))


def _managed_counts() -> dict[str, int]:
    return {m["name"]: m["tables"] for m in introspect.managed_schemas()}


def _schemas_fingerprint(ops) -> str:
    """Hash of the managed schema names; drop_schema also hashes table counts (emptiness matters);
    move_table also hashes the table on both sides of the move."""
    counts = _managed_counts()
    if any(o.get("op") == "drop_schema" for o in ops):
        parts = {"schemas": counts}
    else:
        parts = {"schemas": sorted(counts)}
    for o in ops:
        if o.get("op") == "move_table":
            for s in (o["from_schema"], o["to_schema"]):
                try:
                    parts[f"{s}.{o['table']}"] = introspect.fingerprint([o["table"]], s)
                except introspect.UnknownSchema:
                    parts[f"{s}.{o['table']}"] = "no such schema"
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def _schema_states(ops) -> dict[str, dict]:
    """Snapshots of the schemas a move_table names (what its gate check needs)."""
    names = {s for o in ops if isinstance(o, dict) and o.get("op") == "move_table"
             for s in (o.get("from_schema"), o.get("to_schema"))}
    out = {}
    for n in names:
        try:
            out[n] = introspect.snapshot(n)
        except introspect.UnknownSchema:
            pass
    return out


def _current_fingerprint(change: Change) -> str:
    """Fingerprint the change would get right now ('' when it affects nothing). May raise UnknownSchema."""
    ops = change.operations or change.original_operations
    if _is_database(ops):
        return _schemas_fingerprint(ops)
    if change.affected_tables:
        return introspect.fingerprint(change.affected_tables, change.schema)
    return ""


def _limit_args(ops=None) -> dict:
    return {"managed_schemas": _managed_counts(), "max_schemas": get_config()["limits"]["maxSchemas"],
            "schema_states": _schema_states(ops or [])}


def _mark_stale(change: Change, user, error: str) -> Change:
    change.status = Change.Status.STALE
    change.error = error
    change.save()
    record(user, "change.stale", target=f"#{change.pk}", details={"error": change.error})
    return change


def can_decide(user, change: Change) -> bool:
    if change.source == "script" or change.kind in ("schema", "config", "sql"):
        # structure, rules, settings, roles, raw SQL and every script load: approvers only
        return user.can_approve
    return user.can_approve or (user.can_edit and user.pk == change.requester_id)


def _apply_check(change: Change, res, *, ops_for_tables) -> None:
    change.operations = res.operations
    change.check_result = res.as_dict()
    change.generated_sql = res.sql
    change.diff = res.diff
    change.affected_tables = _affected(ops_for_tables)
    change.fingerprint = _current_fingerprint(change)
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


def _warn_row_counts(res, schema: str) -> None:
    """A delete_rows is checked against the data too: say how many rows it would remove (never blocks)."""
    for i, op in enumerate(res.operations):
        if op["op"] != "delete_rows":
            continue
        try:
            n = introspect.count_where(op["table"], op["where"], schema)
        except psycopg.Error:
            continue
        res.violations.append({
            "rule": "deleteRowCount", "severity": "warn", "op_index": i, "suggestion": None,
            "message": f"delete_rows on {op['table']} will remove {n} row(s)",
        })


def propose(user, raw_ops, *, source: str, prompt: str = "", conversation_id: int | None = None,
            reverts: Change | None = None, schema: str = "master") -> Change:
    if config_ops.has_config(raw_ops):
        if not config_ops.is_config(raw_ops):
            raise ChangeError("invalid", "configuration operations (settings, rules, roles) cannot be mixed with schema or data operations; propose them separately")
        return propose_config(user, raw_ops, source=source, prompt=prompt, conversation_id=conversation_id, reverts=reverts)
    if not user.can_edit:
        raise ChangeError("forbidden", "your role cannot propose changes")
    limits = get_config()["limits"]
    if _is_database(raw_ops):
        schema = "master"  # database ops have no target schema of their own
    else:
        try:
            introspect.require_managed(schema)
        except introspect.UnknownSchema:
            raise ChangeError("invalid", f"schema {schema!r} does not exist or is not managed by Singularity") from None
    is_revert = reverts is not None
    max_ops = max(limits["maxOpsPerProposal"], len(raw_ops)) if is_revert and isinstance(raw_ops, list) \
        else limits["maxOpsPerProposal"]

    res = check.run(
        raw_ops, introspect.snapshot(schema), rule_service.active_rules(),
        actor=user.email, max_ops=max_ops, skip_rules=is_revert,
        allow_bulk=source == "script", schema=schema, **_limit_args(raw_ops),
    )
    if not res.errors:
        _warn_row_counts(res, schema)
    change = Change(
        requester=user, source=source, prompt=prompt, conversation_id=conversation_id,
        kind=_kind(raw_ops), schema=schema, original_operations=raw_ops if isinstance(raw_ops, list) else [], reverts=reverts,
    )
    _apply_check(change, res, ops_for_tables=res.operations)
    change.save()
    record(user, "change.proposed", target=f"#{change.pk}",
           details={"source": source, "kind": change.kind, "status": change.status})
    if change.status == Change.Status.REJECTED:
        record(user, "change.rejected_by_gate", target=f"#{change.pk}", details=change.check_result)
    return change


RAW_SQL_WARNING = "Rules were not checked: this is raw SQL."
MIN_REASON = 10


def propose_sql(user, sql: str, reason: str, *, schema: str = "master", source: str = "mcp",
                conversation_id: int | None = None) -> Change:
    """Queue raw SQL for an approver. Nothing runs for real here: parse, refuse, warn and a rolled-back dry run."""
    if not user.can_edit:
        raise ChangeError("forbidden", "your role cannot propose changes")
    cfg = get_config()
    if not cfg["sql"]["enabled"]:
        raise ChangeError("invalid", "raw SQL is switched off (setting sql.enabled); use propose_change operations")
    if not isinstance(reason, str) or len(reason.strip()) < MIN_REASON:
        raise ChangeError("invalid", f"say why in 'reason' (at least {MIN_REASON} characters): the approver reads it")
    if not isinstance(sql, str):
        raise ChangeError("invalid", "sql must be text")
    try:
        introspect.require_managed(schema)
    except introspect.UnknownSchema:
        raise ChangeError("invalid", f"schema {schema!r} does not exist or is not managed by Singularity; call list_schemas") from None
    limits = cfg["limits"]
    try:
        statements = rawsql.parse_statements(sql, max_bytes=limits["maxSqlBytes"], max_statements=limits["maxSqlStatements"])
        diff = rawsql.dry_run(statements, schema)
        violations = [{"rule": "rawSql", "severity": "warn", "message": RAW_SQL_WARNING, "op_index": None, "suggestion": None},
                      *rawsql.warnings_for(statements, schema)]
    except rawsql.RawSqlError as exc:
        raise ChangeError("invalid", exc.message) from exc
    op = {"op": "raw_sql", "sql": sql, "reason": reason.strip()}
    change = Change(
        requester=user, source=source, prompt=reason.strip(), conversation_id=conversation_id, kind="sql", schema=schema,
        original_operations=[op], operations=[op], generated_sql=[s.text for s in statements], diff=diff,
        check_result={"errors": [], "violations": violations, "autoAdded": []}, affected_tables=[],
        fingerprint=introspect.schema_fingerprint(schema), rules_hash="", status=Change.Status.PENDING,
        reverse_operations=None,
    )
    change.save()
    record(user, "change.proposed", target=f"#{change.pk}", details={"source": source, "kind": "sql", "status": change.status})
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
        try:
            moved = (
                (bool(change.fingerprint) and _current_fingerprint(change) != change.fingerprint)
                or (not is_revert and rule_service.rules_hash() != change.rules_hash)
            )
            state = introspect.snapshot(change.schema)
        except introspect.UnknownSchema:
            return _mark_stale(change, user, f"schema {change.schema!r} no longer exists (it was renamed or dropped); propose the change again")
        recheck = check.run(
            change.operations, state, rule_service.active_rules(), actor=change.requester.email,
            max_ops=max(len(change.operations), 1), skip_rules=True,
            allow_bulk=change.source == "script", schema=change.schema, **_limit_args(change.operations),
        )
        if moved or recheck.errors:
            return _mark_stale(change, user, "; ".join(recheck.errors) or "the schema or the rules changed since this was checked")

        change.decided_by, change.decided_at = user, timezone.now()
        record(user, "change.approved", target=f"#{change.pk}")

        if change.kind == "schema":
            # create_schema touches nothing existing and drop_schema only drops an empty schema: no backup.
            # rename_schema backs up the schema being renamed (taken under its old name).
            if _is_database(change.operations):
                to_back_up = [o["schema"] for o in change.operations if o["op"] == "rename_schema"]
                for o in change.operations:
                    if o["op"] == "move_table":
                        to_back_up += [o["from_schema"], o["to_schema"]]
            else:
                to_back_up = [change.schema]
            for name in to_back_up:
                try:
                    path = backup_schema(name, f"change-{change.pk}", cfg["backups"]["retentionDays"])
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
                row_source=_script_row_source(change), schema=change.schema,
            )
        except executor.ApplyError as exc:
            return _fail(change, user, str(exc))

        change.status = Change.Status.APPLIED
        change.applied_at = timezone.now()
        change.reverse_operations = result.reverse_ops
        change.error = ""
        change.save()
        _follow_renames(change)
        record(user, "change.applied", target=f"#{change.pk}",
               details={"kind": change.kind, "tables": change.affected_tables, "reversible": result.reverse_ops is not None})
        return change


def _follow_renames(change: Change) -> None:
    """After an applied rename_schema, duplicate groups of that schema follow the new name (search reindexes itself)."""
    if change.kind != "schema":
        return
    from duplicates.models import DuplicateGroup
    from duplicates.service import group_key
    for o in change.operations:
        if o.get("op") == "drop_schema":
            DuplicateGroup.objects.filter(schema=o["schema"]).delete()  # the schema is gone, so are its groups
        if o.get("op") == "rename_schema":
            # groups left over from an earlier schema of that name would collide on (schema, table, key)
            DuplicateGroup.objects.filter(schema=o["new_name"]).delete()
            for g in DuplicateGroup.objects.filter(schema=o["schema"]):
                g.schema = o["new_name"]
                g.key = group_key(g.table_name, g.member_ids, o["new_name"])  # keys embed the schema name
                g.save(update_fields=["schema", "key"])


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
        try:
            res = check.run(
                change.original_operations, introspect.snapshot(change.schema), rule_service.active_rules(),
                actor=change.requester.email, max_ops=max(len(change.original_operations), 1), skip_rules=is_revert,
                schema=change.schema, **_limit_args(change.original_operations),
            )
            _apply_check(change, res, ops_for_tables=res.operations)
        except introspect.UnknownSchema:
            _mark_stale(change, user, f"schema {change.schema!r} no longer exists (it was renamed or dropped); propose the change again")
            return change
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
        rev = propose(user, orig.reverse_operations, source="revert", prompt=f"Revert change #{orig.pk}",
                      reverts=orig, schema=orig.schema)
    if rev.status != Change.Status.PENDING:
        return rev
    rev = approve(user, rev.pk)  # an approver may approve their own change
    if rev.status == Change.Status.APPLIED:
        record(user, "change.reverted", target=f"#{orig.pk}", details={"by": f"#{rev.pk}"})
    return rev


def list_changes(user, *, status: str | None = None, kind: str | None = None, schema: str | None = None,
                 mine: bool = False, limit: int = 50) -> tuple[list[Change], bool]:
    """Change history, newest first, and whether more exist beyond `limit`.

    `schema` is not checked against the managed list on purpose: history outlives a database that was
    renamed or dropped. An unknown filter value simply matches nothing.
    """
    qs = Change.objects.select_related("requester", "decided_by")
    if status:
        qs = qs.filter(status=status)
    if schema is not None:
        qs = qs.filter(schema=schema)
    if kind:
        qs = qs.filter(kind=kind)
    if mine:
        qs = qs.filter(requester=user)
    rows = list(qs[: limit + 1])
    return rows[:limit], len(rows) > limit


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
        "schema": change.schema,
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
