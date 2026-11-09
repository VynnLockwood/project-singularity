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


def validate(user, script_id: int, mapping: dict | None, schema: str = "master") -> dict:
    _require_edit(user)
    script = Script.objects.filter(pk=script_id).first()
    if script is None:
        raise storage.ScriptError(f"script #{script_id} does not exist")
    return validate_mod.validate(script, mapping, schema)


def submit(user, script_id: int, note: str = "", schema: str | None = None):
    """Turn a validated script into a pending Change (script row locked while deciding).

    The change targets the schema the script was validated against; a different `schema` is refused."""
    _require_edit(user)
    with transaction.atomic():
        script = (Script.objects.select_for_update().filter(pk=script_id).first())
        if script is None:
            raise storage.ScriptError(f"script #{script_id} does not exist")
        if script.expires_at and script.expires_at <= timezone.now():
            raise storage.ScriptError(f"script #{script_id} has expired")
        if script.status != Script.Status.VALIDATED:
            raise storage.ScriptError(
                f"script #{script.pk} is {script.status}, not validated; validate it first")
        validated = (script.report or {}).get("schema", "master")
        if schema is not None and schema != validated:
            raise storage.ScriptError(
                f"script #{script.pk} was validated for schema {validated!r}, not {schema!r}; validate it again")
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
    mapped = apply_mapping(parsed, script.mapping)
    if mapped.issues:
        raise storage.ScriptError(f"script #{script.pk} no longer maps cleanly: {mapped.issues[0].message}")
    out = copy.deepcopy(mapped.ops)
    for o in out:
        if o.get("op") == "bulk_insert":
            o["source"]["script_id"] = script.pk
    return out
