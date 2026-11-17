"""Script service layer: upload, validate, submit (spec section 2, script intake)."""
from __future__ import annotations

import copy

from django.db import transaction
from django.utils import timezone

from approvals import service as approvals_service
from schema import introspect

from . import storage, validate as validate_mod
from .mapping import apply_mapping
from .models import Script
from .sqlparse import parse_script


def _require_edit(user) -> None:
    if not user.can_edit:
        raise PermissionError("your role cannot manage scripts")


def upload(user, name: str, data: bytes) -> Script:
    _require_edit(user)
    return storage.save(user, name, data)


IMPORT_ONLY = "script #{} belongs to an import; check and submit it from the Import tab"
NOT_FOUND = "script #{} does not exist"
SOMEONE_ELSE = "this script belongs to someone else"


def script_database(script: Script) -> str | None:
    """The database a script is known to be for: the one it last passed validation against; None before that."""
    v = (script.report or {}).get("validated_schema") if isinstance(script.report, dict) else None
    return v if isinstance(v, str) and v else None


def hidden_from(user, script: Script | None) -> bool:
    """True when `user` must be told the script does not exist: it is someone else's and its database is not
    known yet or is one they cannot read. A person's own scripts are always theirs to see."""
    from accounts import access

    if script is None:
        return True
    if script.owner_id == user.pk:
        return False
    db = script_database(script)
    return db is None or not access.can(user, db, "viewer")


def check_reach(user, script: Script) -> None:
    """Someone else's script whose database they can read: a global approver who is approver there may act on
    it. Anyone else gets the old refusal (PermissionError); an approver below approver there gets the role text."""
    from accounts import access

    if script.owner_id == user.pk:
        return
    if not user.can_approve:
        raise PermissionError(SOMEONE_ELSE)
    access.require(user, script_database(script), "approver")


def visible(user):
    """The scripts `user` may list or open (without import runs' scripts): their own, and for a global approver
    others' scripts validated for a database where they are approver. Filtered in the query."""
    from django.db.models import Q

    from accounts import access
    from accounts.models import Role

    qs = Script.objects.exclude(kind="import")
    if user.is_active and user.role == Role.ADMIN:
        return qs
    if not user.can_approve:
        return qs.filter(owner=user)
    dbs = [e["name"] for e in access.effective_schemas(user) if e["role"] in (Role.APPROVER, Role.ADMIN)]
    return qs.filter(Q(owner=user) | Q(report__validated_schema__in=dbs))


def validate(user, script_id: int, mapping: dict | None, schema: str | None = None) -> dict:
    """Validate a script for `schema` (None: the one the file names if the user can reach it, else master).
    The user needs editor on the database used, checked before any of its data is read."""
    from accounts import access

    _require_edit(user)
    script = Script.objects.filter(pk=script_id).first()
    if hidden_from(user, script):
        raise storage.ScriptError(NOT_FOUND.format(script_id))
    if script.kind == "import":
        raise storage.ScriptError(IMPORT_ONLY.format(script.pk))
    check_reach(user, script)
    if schema is not None:
        access.require(user, schema, "editor")
    return validate_mod.validate(script, mapping, schema, user=user)


def submit(user, script_id: int, note: str = "", schema: str | None = None, *, _import_run: bool = False):
    """Turn a validated script into a pending Change (script row locked while deciding).

    The change targets the schema the script was validated against; a different `schema` is refused.
    Import scripts (kind "import") are refused unless the import run submits them (`_import_run=True`)."""
    from accounts import access

    _require_edit(user)
    with transaction.atomic():
        script = (Script.objects.select_for_update().filter(pk=script_id).first())
        if script is None or (not _import_run and hidden_from(user, script)):
            raise storage.ScriptError(NOT_FOUND.format(script_id))
        if script.kind == "import" and not _import_run:
            raise storage.ScriptError(IMPORT_ONLY.format(script.pk))
        if script.expires_at and script.expires_at <= timezone.now():
            raise storage.ScriptError(f"script #{script_id} has expired")
        if script.status != Script.Status.VALIDATED:
            raise storage.ScriptError(
                f"script #{script.pk} is {script.status}, not validated; validate it first")
        validated = (script.report or {}).get("validated_schema", "master")
        if schema is not None and schema != validated:
            raise storage.ScriptError(
                f"script #{script.pk} was validated for schema {validated!r}, not {schema!r}; validate it again")
        access.require(user, validated, "editor")   # before the database is read for the ops
        ops = _bound_ops(script)  # ScriptError when the file is missing/tampered

        change = approvals_service.propose(user, ops, source="script", prompt=note, schema=validated)
        script.status = Script.Status.SUBMITTED
        script.change = change
        script.save(update_fields=["status", "change"])
        return change


def _bound_ops(script: Script) -> list[dict]:
    """The script's stored mapping -> ops bound to the stored script; reads (and checksums) the file."""
    text = storage.read(script).decode("utf-8")
    parsed = parse_script(text)
    schema = (script.report or {}).get("validated_schema", "master")
    mapped = apply_mapping(parsed, script.mapping, introspect.external_state(schema=schema))
    if mapped.issues:
        raise storage.ScriptError(f"script #{script.pk} no longer maps cleanly: {mapped.issues[0].message}")
    out = copy.deepcopy(mapped.ops)
    for o in out:
        if o.get("op") == "bulk_insert":
            o["source"]["script_id"] = script.pk
    return out


def trim_report(report: dict) -> dict:
    """A validation report small enough for an AI client: at most 20 errors, 20 skipped statements and 10 sample orphans per table."""
    out = dict(report)
    out["errors"] = report.get("errors", [])[:20]
    skipped = report.get("skipped", [])
    out["skipped"] = skipped[:20]
    out["skipped_truncated"] = report.get("skipped_truncated", 0) + max(0, len(skipped) - 20)
    out["tables"] = [{**t, "sample_orphans": t.get("sample_orphans", [])[:10]} for t in out.get("tables", [])]
    return out
