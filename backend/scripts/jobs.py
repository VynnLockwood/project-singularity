"""Validation as a background job: a child process runs `manage.py validate_script <id>`; the script's report row
carries {state, phase, heartbeat} so the page can poll. A heartbeat older than STALE_SECONDS reads as failed."""
from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime, timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from . import storage, validate as validate_mod
from .models import Script
from .storage import ScriptError

STALE_SECONDS = 600
BEAT_SECONDS = 30
STOPPED = {"state": "failed", "message": "The check stopped unexpectedly. Try again."}


class Busy(ScriptError):
    """A check of this script is already running (HTTP 409)."""


class Forbidden(ScriptError):
    """The script belongs to someone else (HTTP 403)."""


def _now() -> str:
    return timezone.now().isoformat()


def is_stale(report: dict | None) -> bool:
    """True for a `running` report whose heartbeat is missing or older than STALE_SECONDS."""
    if not report or report.get("state") != "running":
        return False
    try:
        beat = datetime.fromisoformat(report["heartbeat"])
    except (KeyError, TypeError, ValueError):
        return True
    return timezone.now() - beat > timedelta(seconds=STALE_SECONDS)


def _carry(report: dict | None) -> dict:
    """What must survive a running or failed report: the schema the script last passed for (submit needs it)."""
    v = (report or {}).get("validated_schema")
    return {"validated_schema": v} if v else {}


def start(user, script_id: int, mapping: dict | None, schema: str = "master") -> dict:
    if not user.can_edit:
        raise Forbidden("your role cannot manage scripts")
    with transaction.atomic():
        script = Script.objects.select_for_update().filter(pk=script_id).first()
        if script is None:
            raise ScriptError(f"script #{script_id} does not exist")
        if script.owner_id != user.pk and not user.can_approve:
            raise Forbidden("this script belongs to someone else")
        if script.status in (Script.Status.SUBMITTED, Script.Status.EXPIRED):
            raise ScriptError(f"script #{script.pk} is {script.status}; only uploaded or validated scripts can be "
                              "validated")
        current = script.report or {}
        if current.get("state") == "running" and not is_stale(current):
            raise Busy("A check of this script is already running.")
        script.report = {"state": "running", "phase": "queued", "heartbeat": _now(),
                         "job": {"mapping": mapping, "schema": schema}, **_carry(current)}
        script.save(update_fields=["report"])
    try:
        proc = subprocess.Popen([sys.executable, "manage.py", "validate_script", str(script_id)],
                                cwd=settings.BASE_DIR, start_new_session=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        _fail(script_id, f"Could not start the check: {exc}")
        raise ScriptError("Could not start the check. Try again.") from exc
    if proc is not None and hasattr(proc, "wait"):
        threading.Thread(target=proc.wait, daemon=True).start()  # reap the child when it ends
    return {"state": "running"}


def _fail(script_id: int, message: str) -> None:
    script = Script.objects.filter(pk=script_id).first()
    if script is not None:
        Script.objects.filter(pk=script_id).update(report={"state": "failed", "message": message[:500],
                                                           **_carry(script.report)})


def _beat(script_id: int) -> None:
    """Move only the heartbeat, and only while the report still says running (never clobbers a finished report)."""
    with connection.cursor() as cur:
        cur.execute("UPDATE scripts_script SET report = jsonb_set(report, '{heartbeat}', to_jsonb(%s::text)) "
                    "WHERE id = %s AND report->>'state' = 'running'", [_now(), script_id])


def run(script_id: int, beat_seconds: float = BEAT_SECONDS) -> None:
    """The body of the validate_script command."""
    script = Script.objects.filter(pk=script_id).first()
    if script is None:
        return
    job = (script.report or {}).get("job") or {}
    stop = threading.Event()

    def beater():
        try:
            while not stop.wait(beat_seconds):
                _beat(script_id)
        finally:
            connection.close()

    def progress(phase: str) -> None:
        with connection.cursor() as cur:
            cur.execute("UPDATE scripts_script SET report = report || jsonb_build_object('phase', %s::text, "
                        "'heartbeat', %s::text) WHERE id = %s AND report->>'state' = 'running'",
                        [phase, _now(), script_id])

    t = threading.Thread(target=beater, daemon=True)
    t.start()
    try:
        report = validate_mod.validate(script, job.get("mapping"), job.get("schema") or "master", progress=progress)
        stop.set()
        t.join()
        report["state"] = "done"
        Script.objects.filter(pk=script_id).update(report=report)
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the page must see a failed state, not a hang
        stop.set()
        t.join()
        _fail(script_id, f"The check failed: {exc}")
