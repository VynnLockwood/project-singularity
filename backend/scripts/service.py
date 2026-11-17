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


# every place a script's stored report names a database: the one it passed for, the one the last check used,
# the one the file names, and the one a queued or running check was asked for
REPORT_DATABASE_KEYS = (("validated_schema",), ("schema",), ("detected_schema",), ("job", "schema"))


def _at(report, path):
    cur = report
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def script_databases(script: Script) -> set[str] | None:
    """Every database a script's stored data names (see REPORT_DATABASE_KEYS). None when it cannot be told: no
    validation has passed yet (no `validated_schema`), or a stored name is not a non-empty string."""
    report = script.report if isinstance(script.report, dict) else {}
    if not isinstance(report.get("validated_schema"), str) or not report["validated_schema"]:
        return None
    out = set()
    for path in REPORT_DATABASE_KEYS:
        v = _at(report, path)
        if v is None:
            continue
        if not isinstance(v, str) or not v:
            return None
        if path == ("detected_schema",) and v not in _managed():
            continue   # a name in the file that is no database here (e.g. public) names none of ours
        out.add(v)
    return out


def _managed() -> set[str]:
    return {m["name"] for m in introspect.managed_schemas()}


def hidden_from(user, script: Script | None) -> bool:
    """True when `user` must be told the script does not exist: it is someone else's and the databases its
    report names are not known, or include one they cannot read. A person's own scripts are always theirs."""
    from accounts import access

    if script is None:
        return True
    if script.owner_id == user.pk:
        return False
    dbs = script_databases(script)
    return dbs is None or not all(access.can(user, db, "viewer") for db in dbs)


def check_reach(user, script: Script) -> None:
    """Someone else's script whose databases they can read: a global approver who is approver on every one of
    them may act on it. Anyone else gets the old refusal (PermissionError); an approver below approver on one of
    them gets the role text. Call after `hidden_from`."""
    from accounts import access

    if script.owner_id == user.pk:
        return
    if not user.can_approve:
        raise PermissionError(SOMEONE_ELSE)
    for db in sorted(script_databases(script) or ()):
        access.require(user, db, "approver")


def visible(user):
    """The scripts `user` may list or open (without import runs' scripts): their own, and for a global approver
    others' scripts whose report names only databases where they are approver (`validated_schema` required).
    Filtered in the query, like `hidden_from` and `check_reach` decide one script."""
    from django.db.models import Q

    from accounts import access
    from accounts.models import Role

    qs = Script.objects.exclude(kind="import")
    if user.is_active and user.role == Role.ADMIN:
        return qs
    if not user.can_approve:
        return qs.filter(owner=user)
    dbs = [e["name"] for e in access.effective_schemas(user) if e["role"] in (Role.APPROVER, Role.ADMIN)]
    others = Q(report__validated_schema__in=dbs)
    for path in REPORT_DATABASE_KEYS[1:]:
        key = "report__" + "__".join(path)
        ok = Q(**{f"{key}__isnull": True}) | Q(**{key: None}) | Q(**{f"{key}__in": dbs})
        if path == ("detected_schema",):   # a name in the file that is no database here names none of ours
            ok |= ~Q(**{f"{key}__in": sorted(_managed())})
        others &= ok
    return qs.filter(Q(owner=user) | others)


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
        if not _import_run:
            check_reach(user, script)   # the same reach as reading it: owner, or approver on its databases
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
