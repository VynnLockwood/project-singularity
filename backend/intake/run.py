"""The import run: Profiler -> Matcher -> Designer <-> Critic, as a background child process like scripts.jobs.

`start` takes a finished import upload and spawns `manage.py run_import <id>`; the chat API key travels only in
the child's environment (SINGULARITY_IMPORT_KEY), never through the database. `execute` is the command's body and
picks up where the state says:
    profiling -> the whole relay;  designing -> the Designer loop (after an answer);  checking -> the Critic only
    (after the person's edits). Each role's turn is one ImportMessage.

Plan (run.plan):
    {"mapping": <scripts mapping>, "filters": {sheet: [{column, op, value}]}, "reasons": {path: str},
     "questions": [str], "skip": {sheet: [1-based original rows]},   # effective: already-existing + picked
     "picked": {sheet: [rows]}}                                      # the person's own skips
Report (run.report), written by the Critic:
    {"ok": bool, "problems": [str] (<= 20, each <= 300 chars), "scriptId": int | None,
     "tables": {sheet: {"target", "rows", "new", "exists", "filtered", "skipped", "maybe": [{row, id, score,
                existing}]}},
     "badRows": [{"line", "message"}], "dropped": [{"sheet", "column", "reason"}], "warnings": [str]}
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import threading
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from accounts.models import User
from approvals import service as approvals
from approvals.models import Change
from config.siconfig import get_config
from gate.operations import IDENT_RE
from schema import introspect, roles
from scripts import service as script_service
from scripts import storage, uploads
from scripts import validate as validate_mod
from scripts.models import Script, UploadSession
from scripts.storage import ScriptError
from settings_app import services as rule_services

from . import design, reader
from . import match as matcher
from . import script as writer
from .models import ImportMessage, ImportRun
from .profile import profile as profile_source

KEY_ENV = "SINGULARITY_IMPORT_KEY"
STALE_SECONDS = 600
BEAT_SECONDS = 30
RUN_TTL = timedelta(hours=24)
MAX_PROBLEMS = 20
MAX_PROBLEM_CHARS = 300
MAX_TEXT = 2000
S = ImportRun.State
RUNNING = (S.PROFILING, S.MATCHING, S.DESIGNING, S.CHECKING)
CLOSED = (S.FAILED, S.SUBMITTED)


BUSY = "You already have an import open for this database. Finish or cancel it first."


class RunError(Exception):
    """code: forbidden | not_found | bad_state | busy | invalid (HTTP 403 / 404 / 409 / 409 / 400)."""

    def __init__(self, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


def _busy(other: ImportRun) -> RunError:
    """The busy refusal names the open run, so the page can lead the person back to it."""
    return RunError("busy", BUSY, {"runId": other.pk, "schema": other.schema})


# ---------------------------------------------------------------- small helpers

def _clip(s, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 3] + "..."


def _say(run: ImportRun, role: str, text: str, details: dict | None = None) -> None:
    ImportMessage.objects.create(run=run, role=role, text=_clip(text, MAX_TEXT), details=details or {})


def _set(run: ImportRun, **fields) -> None:
    for k, v in fields.items():
        setattr(run, k, v)
    run.heartbeat = timezone.now()
    run.save(update_fields=[*fields, "heartbeat"])


def _fail(run: ImportRun, text: str) -> None:
    _set(run, state=S.FAILED)
    _say(run, ImportMessage.Role.SYSTEM, text)


def is_stale(run: ImportRun) -> bool:
    if run.state not in RUNNING:
        return False
    return run.heartbeat is None or timezone.now() - run.heartbeat > timedelta(seconds=STALE_SECONDS)


def _refresh(run: ImportRun) -> ImportRun:
    """A run whose child stopped beating reads as failed (and is stored so)."""
    if is_stale(run):
        if ImportRun.objects.filter(pk=run.pk, state=run.state, heartbeat=run.heartbeat).update(state=S.FAILED):
            _say(run, ImportMessage.Role.SYSTEM, "The import stopped unexpectedly. Start it again.")
        run.refresh_from_db()
    return run


def _remove_file(path: str) -> None:
    if not path:
        return
    root = os.path.realpath(storage.imports_dir())
    if os.path.dirname(os.path.realpath(path)) != root:
        return      # only ever delete our own raw files
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _drop_script(script_id) -> None:
    """Retire a superseded Critic script: file gone, row marked expired (never a submitted one)."""
    if not script_id:
        return
    s = Script.objects.filter(pk=script_id).exclude(status=Script.Status.SUBMITTED).first()
    if s is None:
        return
    if Script.objects.filter(pk=s.pk, status=s.status).update(status=Script.Status.EXPIRED, path="") and s.path:
        try:
            os.unlink(os.path.join(settings.SCRIPT_DIR, s.path))
        except FileNotFoundError:
            pass


def _script_name(run_id: int) -> str:
    return f"import-{run_id}.sql"


def _drop_run_scripts(run_id: int) -> None:
    """Retire every Critic script of a run (all but a submitted one)."""
    for pk in Script.objects.filter(kind="import", name=_script_name(run_id)).values_list("pk", flat=True):
        _drop_script(pk)


# ---------------------------------------------------------------- access

def _require_edit(user) -> None:
    if not getattr(user, "can_edit", False):
        raise RunError("forbidden", "your role cannot import files")


def _denied(exc) -> RunError:
    return RunError("access_denied", str(exc))


def _check_owner(user, run: ImportRun, level: str = "viewer") -> None:
    """A run in a database the user cannot reach does not exist for them (their own included); someone else's
    run needs a global approver who is approver on its database; `level` is what the call needs there
    (editor to change it). Readable but below that: the role refusal (`access_denied`, 403)."""
    from accounts import access

    if not access.can(user, run.schema, "viewer"):
        raise RunError("not_found", f"import #{run.pk} does not exist")
    if run.owner_id != user.pk:
        if not user.can_approve:
            raise RunError("forbidden", "this import belongs to someone else")
        level = "approver"
    if not access.can(user, run.schema, level):
        try:
            access.require(user, run.schema, level)
        except (access.AccessDenied, introspect.UnknownSchema) as exc:
            raise _denied(exc) from None


def get(user, run_id: int) -> ImportRun:
    _require_edit(user)
    run = ImportRun.objects.filter(pk=run_id).first()
    if run is None:
        raise RunError("not_found", f"import #{run_id} does not exist")
    _check_owner(user, run)
    return _refresh(run)


def _locked(user, run_id: int, level: str = "editor") -> ImportRun:
    _require_edit(user)
    run = ImportRun.objects.select_for_update().filter(pk=run_id).first()
    if run is None:
        raise RunError("not_found", f"import #{run_id} does not exist")
    _check_owner(user, run, level)
    return _refresh(run)


def _open_run(user, schema: str, exclude: int | None = None) -> ImportRun | None:
    for r in (ImportRun.objects.filter(owner=user, schema=schema, expires_at__gt=timezone.now())
              .exclude(state__in=CLOSED).exclude(pk=exclude)):
        if _refresh(r).state not in CLOSED:
            return r
    return None


# ---------------------------------------------------------------- the child process

def _spawn(run: ImportRun, api_key: str) -> None:
    env = {k: v for k, v in os.environ.items() if k != KEY_ENV}
    if api_key:
        env[KEY_ENV] = api_key
    try:
        proc = subprocess.Popen([sys.executable, "manage.py", "run_import", str(run.pk)],
                                cwd=settings.BASE_DIR, start_new_session=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    except OSError as exc:
        _fail(run, f"Could not start the import: {exc}")
        raise RunError("bad_state", "Could not start the import. Try again.") from exc
    if proc is not None and hasattr(proc, "wait"):
        threading.Thread(target=proc.wait, daemon=True).start()  # reap the child when it ends


def start(user, upload_id: int, schema: str, prompt: str, api_key: str = "") -> ImportRun:
    from accounts import access

    _require_edit(user)
    try:
        access.require(user, schema, "editor")   # unreachable reads like missing
        introspect.require_managed(schema)
    except introspect.UnknownSchema as exc:
        raise RunError("invalid", str(exc)) from None
    except access.AccessDenied as exc:
        raise _denied(exc) from None
    purge_expired()
    path = None
    try:
        with transaction.atomic():
            User.objects.select_for_update().filter(pk=user.pk).first()   # one start at a time per person
            other = _open_run(user, schema)
            if other is not None:
                raise _busy(other)
            session = UploadSession.objects.filter(pk=upload_id, owner=user).first()
            path = uploads.complete_raw(user, upload_id)
            now = timezone.now()
            run = ImportRun.objects.create(owner=user, schema=schema, prompt=prompt or "", file_path=path,
                                           file_name=session.name if session else os.path.basename(path),
                                           state=S.PROFILING, heartbeat=now, created_at=now,
                                           expires_at=now + RUN_TTL)
    except uploads.NotFound as exc:
        raise RunError("not_found", str(exc)) from None
    except ScriptError as exc:
        raise RunError("invalid", str(exc)) from None
    except BaseException:
        if path:
            _remove_file(path)
        raise
    _spawn(run, api_key)
    return run


def answer(user, run_id: int, text: str, api_key: str = "") -> ImportRun:
    with transaction.atomic():
        run = _locked(user, run_id)
        if run.state != S.WAITING_FOR_ANSWER:
            raise RunError("bad_state", f"import #{run.pk} is {run.state}, not waiting for an answer")
        run.answers = [*run.answers, {"questions": (run.plan or {}).get("questions") or [], "text": text,
                                      "at": timezone.now().isoformat()}]
        _set(run, answers=run.answers, state=S.DESIGNING)
        _say(run, ImportMessage.Role.PERSON, text)
    _spawn(run, api_key)
    return run


def _rows_arg(skip) -> dict:
    if skip in (None, ""):
        return {}
    if not isinstance(skip, dict):
        raise RunError("invalid", "skip must be an object of sheet -> row numbers")
    out = {}
    for name, rows in skip.items():
        if not isinstance(rows, list):
            raise RunError("invalid", f"skip.{name} must be a list of row numbers")
        nums = set()
        for v in rows:
            try:
                n = int(v) if not isinstance(v, bool) else None
            except (TypeError, ValueError):
                n = None
            if n is None or n < 1 or (isinstance(v, float) and v != n):
                raise RunError("invalid", f"skip.{name}: {v!r} is not a row number")
            nums.add(n)
        out[str(name)] = sorted(nums)
    return out


def recheck(user, run_id: int, mapping, filters, skip) -> ImportRun:
    """The person's edits: store them and run the Critic only (no Designer)."""
    picked = _rows_arg(skip)
    filters = {} if filters is None else filters
    problems = design.check_plan(mapping, filters)
    if problems:
        raise RunError("invalid", "; ".join(problems))
    with transaction.atomic():
        User.objects.select_for_update().filter(pk=user.pk).first()   # the same lock as start
        run = _locked(user, run_id)
        if run.state not in (S.READY, S.FAILED) or not run.profile:
            raise RunError("bad_state", f"import #{run.pk} is {run.state}; only ready or failed imports can be "
                                        "checked again")
        other = _open_run(run.owner, run.schema, exclude=run.pk) if run.state == S.FAILED else None
        if other is not None:
            raise _busy(other)              # a failed run must not reopen beside another open one
        plan = {**(run.plan or {}), "mapping": mapping, "filters": filters, "picked": picked, "questions": []}
        _set(run, plan=plan, state=S.CHECKING)
    _spawn(run, "")
    return run


def submit(user, run_id: int, note: str = ""):
    with transaction.atomic():
        run = _locked(user, run_id)
        if run.state != S.READY:
            raise RunError("bad_state", f"import #{run.pk} is {run.state}, not ready")
        _set(run, state=S.CHECKING)
    try:
        sources, profiles = _load(run)
        report = _critic(run, sources, profiles, run.plan)
    except Exception as exc:  # noqa: BLE001 - the run must not be left in checking
        _fail(run, f"The final check failed: {exc}")
        raise RunError("invalid", f"the final check failed: {exc}") from None
    if not report["ok"]:
        _set(run, report=report, state=S.FAILED)
        _critic_says(run, report)
        raise RunError("invalid", "the plan no longer passes the check: " + "; ".join(report["problems"][:3]))
    try:
        # one transaction: the Change, the script marked submitted and the run's link to it, or none of them
        with transaction.atomic():
            locked = ImportRun.objects.select_for_update().filter(pk=run.pk).first()
            if locked is None or locked.state != S.CHECKING:
                raise RunError("bad_state", f"import #{run.pk} changed while it was being submitted")
            change = script_service.submit(user, report["scriptId"], note or "", schema=run.schema,
                                           _import_run=True)
            if change.status == Change.Status.REJECTED:
                problems = _rejection(change)
                _set(locked, report={**report, "ok": False, "problems": problems, "feedback": problems},
                     state=S.FAILED)
                _say(locked, ImportMessage.Role.SYSTEM, f"The rules gate rejected change #{change.pk}:\n"
                     + "\n".join(f"- {p}" for p in problems), {"changeId": change.pk, "problems": problems})
            else:
                _set(locked, report=report, state=S.SUBMITTED, change=change)
                _say(locked, ImportMessage.Role.SYSTEM, f"Submitted as change #{change.pk}; it waits for an approver.",
                     {"changeId": change.pk})
    except Exception as exc:  # noqa: BLE001 - whatever failed, the run goes back to ready, never stuck in checking
        ImportRun.objects.filter(pk=run.pk, state=S.CHECKING).update(state=S.READY, report=report,
                                                                     heartbeat=timezone.now())
        if isinstance(exc, RunError):
            raise
        if isinstance(exc, (ScriptError, PermissionError)):
            raise RunError("invalid", str(exc)) from None
        if isinstance(exc, approvals.ChangeError):
            raise RunError("invalid" if exc.code == "invalid" else "bad_state", exc.message) from None
        raise RunError("bad_state", f"the submit failed: {_clip(exc, 500)}") from None
    if change.status == Change.Status.REJECTED:
        raise RunError("invalid", "the rules gate rejected the change: " + "; ".join(_rejection(change)[:3]))
    return change


def _rejection(change) -> list[str]:
    """Why the gate rejected a change, as problems."""
    res = change.check_result or {}
    out = []
    for e in res.get("errors") or []:
        _add(out, f"gate: {e}")
    for v in res.get("violations") or []:
        if not isinstance(v, dict) or v.get("severity") == "block":
            _add(out, f"rule {v.get('rule', '?')}: {v.get('message', v)}" if isinstance(v, dict) else f"rule: {v}")
    return out or [change.decision_note or "the rules gate rejected the change"]


def cancel(user, run_id: int) -> None:
    with transaction.atomic():
        run = _locked(user, run_id)
        if run.state not in (S.WAITING_FOR_ANSWER, S.READY, S.FAILED):
            raise RunError("bad_state", f"import #{run.pk} is {run.state}; it can be cancelled once it is waiting "
                                        "for an answer, ready or failed")
        _drop_run_scripts(run.pk)
        _remove_file(run.file_path)
        run.delete()


def purge_expired(now=None) -> int:
    """Runs past expires_at: deleted with their files and Critic scripts. Submitted runs keep their row (the
    recipe needs it) but lose the raw file."""
    now = now or timezone.now()
    n = 0
    for run in ImportRun.objects.filter(expires_at__lte=now):
        if run.state == S.SUBMITTED:
            if run.file_path:
                _remove_file(run.file_path)
                ImportRun.objects.filter(pk=run.pk).update(file_path="")
                n += 1
            continue
        _drop_run_scripts(run.pk)
        _remove_file(run.file_path)
        run.delete()
        n += 1
    return n


# ---------------------------------------------------------------- the body of run_import

def execute(run_id: int, api_key: str = "", beat_seconds: float = BEAT_SECONDS) -> None:
    run = ImportRun.objects.filter(pk=run_id).first()
    if run is None or run.state not in RUNNING:
        return
    stop = threading.Event()

    def beater():
        try:
            while not stop.wait(beat_seconds):
                ImportRun.objects.filter(pk=run_id, state__in=RUNNING).update(heartbeat=timezone.now())
        finally:
            connection.close()

    t = threading.Thread(target=beater, daemon=True)
    t.start()
    try:
        if run.state in (S.PROFILING, S.MATCHING):
            sources, profiles = _profile(run)
            if sources:
                _match(run, sources, profiles)
                _design_loop(run, sources, profiles, api_key)
        elif run.state == S.DESIGNING:
            _design_loop(run, *_load(run), api_key)
        elif run.state == S.CHECKING:
            report = _critic(run, *_load(run), run.plan)
            _set(run, plan=run.plan, report=report, state=S.READY if report["ok"] else S.FAILED)
            _critic_says(run, report)
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the page must see failed, not a hang
        if not ImportRun.objects.filter(pk=run_id).exists():
            _drop_run_scripts(run_id)   # cancelled or purged meanwhile: leave nothing of it behind
            return
        run.refresh_from_db()
        _fail(run, f"The import failed: {_clip(exc, 500)}")
    finally:
        stop.set()
        t.join()


def _read(run: ImportRun) -> list:
    sources = reader.read(run.file_path, get_config()["limits"]["maxScriptRows"])
    if run.file_path.lower().endswith(".csv") and len(sources) == 1:
        # the stored file has a random name: the sheet is named after the person's file
        stem = os.path.splitext(os.path.basename(run.file_name))[0].strip().replace('"', "")
        sources[0]["name"] = stem or sources[0]["name"]
    return sources


def _load(run: ImportRun) -> tuple[list, list]:
    """Sources with their rows (read again from the file) and the stored profiles."""
    sources = _read(run)
    by_name = {p["name"]: p for p in (run.profile or {}).get("sheets") or []}
    samples = min(int(get_config()["import"]["sampleValues"]), design.MAX_SAMPLES)
    return sources, [by_name.get(s["name"]) or profile_source(s, samples) for s in sources]


def _profile(run: ImportRun) -> tuple[list, list]:
    _set(run, state=S.PROFILING)
    try:
        sources = _read(run)
    except reader.ReaderError as exc:
        _fail(run, f"The file could not be read: {exc}")
        return [], []
    sources = [s for s in sources if s["headers"]]
    if not sources:
        _fail(run, "The file has no sheet with a header row.")
        return [], []
    samples = min(int(get_config()["import"]["sampleValues"]), design.MAX_SAMPLES)
    profiles = [profile_source(s, samples) for s in sources]
    lines, details = [], []
    for s, p in zip(sources, profiles):
        cols = ", ".join(f"{c['header']} {c['type']}" + (f" {c['pattern']}" if c.get("pattern") else "")
                         for c in p["columns"])
        line = f"{s['name']}: {p['rows']} rows, {len(p['columns'])} columns ({cols})."
        if s.get("truncated"):
            line += " Only the first rows were read (the row limit)."
        notes = [f"{c['header']}: {c['note']}" for c in p["columns"] if c.get("note")] + list(s.get("notes") or [])
        if notes:
            line += " " + "; ".join(notes) + "."
        lines.append(line)
        details.append({"sheet": s["name"], "rows": p["rows"], "columns": len(p["columns"])})
    _set(run, sources=[{k: v for k, v in s.items() if k != "rows"} for s in sources],
         profile={"sheets": profiles})
    _say(run, ImportMessage.Role.PROFILER, "\n".join(lines), {"sheets": details})
    return sources, profiles


_rekey_recipe = matcher.rekey_recipe    # moved to the matcher; kept under its old name


def _match(run: ImportRun, sources: list, profiles: list) -> None:
    _set(run, state=S.MATCHING)
    cfg = get_config()["import"]
    m = matcher.match(run.schema, sources, profiles, bool(cfg["rowMatch"]))
    lines = []
    for name, s in m["sources"].items():
        if s["tables"]:
            t, score = s["tables"][0]
            line = f"{name}: best table {t} (score {score})."
        else:
            line = f"{name}: no existing table fits; it can become a new table."
        rows = s.get("rows")
        if rows:
            line += (f" Rows: {len(rows['exists'])} already exist in {rows['table']} (skipped), "
                     f"{len(rows['maybe'])} may already exist (loaded unless you skip them).")
        if s.get("refs"):
            line += " References: " + ", ".join(f"{h} -> {r['table']}.{r['key']}" for h, r in s["refs"].items()) + "."
        lines.append(line)
    if m["searchOffline"]:
        lines.append("Search is offline: columns were matched by name only.")
    if m.get("recipe"):
        m["recipe"] = _rekey_recipe(m["recipe"], sources)
    recipe_id = (m.get("recipe") or {}).get("id")
    if recipe_id:
        lines.append(f"Found an approved recipe (#{recipe_id}) for this file shape; it is the starting mapping.")
    _set(run, matches=m, recipe_id=recipe_id)
    _say(run, ImportMessage.Role.MATCHER, "\n".join(lines),
         {"searchOffline": m["searchOffline"], "recipe": recipe_id})


# ---------------------------------------------------------------- Designer

def rules_text() -> str:
    """The enabled rules, as the get_rules MCP tool lists them."""
    rules = [{k: r[k] for k in ("name", "type", "severity", "description", "params")}
             for r in rule_services.active_rules() if r["enabled"]]
    return json.dumps(rules, ensure_ascii=False)


def targets_text(schema: str, matches: dict) -> str:
    """A compact description of the candidate target tables, plus the names of all tables."""
    state = introspect.snapshot(schema)
    hidden = roles.hidden_columns()
    wanted = []
    for s in (matches.get("sources") or {}).values():
        wanted += [t for t, _ in s.get("tables") or []]
        wanted += [r["table"] for r in (s.get("refs") or {}).values()]
    recipe = matches.get("recipe") or {}
    for entry in ((recipe.get("mapping") or {}).get("tables") or {}).values():
        if isinstance(entry, dict) and isinstance(entry.get("target"), str):
            wanted.append(entry["target"])
    lines = []
    for t in dict.fromkeys(wanted):
        if t not in state:
            continue
        st = state[t]
        cols = ", ".join(f"{c} {d['type']}{'' if d['nullable'] else ' not null'}"
                         for c, d in st["columns"].items() if c not in hidden)
        keys = ["+".join(sorted(k)) for k in st["keys"] if st["pk"] not in k]
        d = introspect.describe_table(t, schema) or {}
        fks = [c["definition"] for c in d.get("constraints") or [] if c.get("kind") == "foreign_key"
               or str(c.get("definition", "")).startswith("FOREIGN KEY")]
        line = f"{t} (primary key {st['pk']}" + (f"; unique: {', '.join(sorted(keys))}" if keys else "") + ")"
        line += f": {cols}"
        if fks:
            line += "; " + "; ".join(fks)
        if st.get("comment"):
            line += f" -- {st['comment']}"
        lines.append(line)
    lines.append("All tables: " + (", ".join(sorted(state)) or "(none)"))
    return "\n".join(lines)


def _summary(name: str, entry, filters: list, reasons: dict) -> str:
    if not isinstance(entry, dict):
        return f"{name}: (no mapping)"
    head = f"{name} -> {entry.get('target', name)}"
    if reasons.get(name):
        head += f" ({reasons[name]})"
    parts = [f"{t} <- {s}" for t, s in (entry.get("columns") or {}).items()] if isinstance(
        entry.get("columns"), dict) else []
    for r in entry.get("refs") or []:
        if isinstance(r, dict):
            parts.append(f"{r.get('column')} <- {r.get('source_column')} (ref {r.get('parent')}.{r.get('parent_key')})")
    for c in entry.get("add_columns") or []:
        if isinstance(c, dict):
            parts.append(f"new column {c.get('name')} {c.get('type')}")
    if entry.get("drop"):
        parts.append("drop " + ", ".join(map(str, entry["drop"])))
    out = head + ": " + "; ".join(parts) if parts else head
    for f in filters or []:
        out += f"\n  only rows where {f.get('column')} {f.get('op')}" + (
            f" {json.dumps(f.get('value'), ensure_ascii=False)}" if f.get("op") != "not_empty" else "")
    return out


def _designer_says(run: ImportRun, out: dict) -> None:
    if out["questions"]:
        text = "Questions before I go on:\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(out["questions"], 1))
    else:
        tables = (out["mapping"].get("tables") or {})
        lines = [_summary(n, e, (out["filters"] or {}).get(n), out["reasons"]) for n, e in tables.items()]
        text = f"Round {run.round}:\n" + ("\n".join(lines) or "(an empty mapping)")
    _say(run, ImportMessage.Role.DESIGNER, text,
         {"round": run.round, "reasons": out["reasons"], "questions": out["questions"]})


def _feedback(report: dict) -> list[str]:
    """What the Designer is told about a failed check: the model-safe problems (no cell values)."""
    if not report or report.get("ok"):
        return []
    return list(report.get("feedback") or [])


def _nothing_left(run: ImportRun, plan: dict, report: dict) -> None:
    """A plan that is fine but has nothing to load: the run ends here, the Designer is not asked again."""
    _set(run, plan=plan, report=report, state=S.FAILED)
    _critic_says(run, report)


def _design_loop(run: ImportRun, sources: list, profiles: list, api_key: str) -> None:
    cfg = get_config()["import"]
    max_rounds = int(cfg["maxRounds"])
    report = run.report or {}
    feedback = _feedback(report)
    recipe = (run.matches or {}).get("recipe")
    if recipe and run.round == 0 and not report:
        # the recipe is re-checked first: a target renamed or dropped since its approval is drift
        filters = recipe.get("filters") if isinstance(recipe.get("filters"), dict) else {}
        recipe_plan = {"mapping": recipe.get("mapping") or {}, "filters": filters}
        drift = _critic(run, sources, profiles, recipe_plan)
        if drift.get("nothing"):
            _nothing_left(run, {**recipe_plan, "reasons": {}, "questions": [],
                                "picked": (run.plan or {}).get("picked") or {}, "skip": recipe_plan.get("skip")},
                          drift)
            return
        if not drift["ok"]:
            _set(run, report=drift)
            _say(run, ImportMessage.Role.CRITIC,
                 "The saved recipe no longer fits this database (drift):\n" + _problem_lines(drift),
                 {"ok": False, "drift": True, "problems": drift["problems"]})
            feedback = _feedback(drift)
        else:
            _drop_script(drift.get("scriptId"))
    rules, targets = rules_text(), targets_text(run.schema, run.matches or {})
    while run.round < max_rounds:
        _set(run, state=S.DESIGNING, round=run.round + 1)
        try:
            out = design.ask(run.prompt, profiles, run.matches or {}, rules, targets, run.answers, feedback,
                             api_key)
        except design.DesignError as exc:
            _say(run, ImportMessage.Role.DESIGNER, f"I could not produce a plan: {exc}.", {"round": run.round})
            _fail(run, f"The Designer stopped: {exc}.")
            return
        picked = (run.plan or {}).get("picked") or {}
        plan = {**out, "picked": picked}
        if out["questions"]:
            # a question turn does not use up a round
            _set(run, plan=plan, state=S.WAITING_FOR_ANSWER, round=run.round - 1)
            _designer_says(run, out)
            return
        _set(run, plan=plan, state=S.CHECKING)
        _designer_says(run, out)
        report = _critic(run, sources, profiles, plan)
        if report.get("nothing"):
            _nothing_left(run, plan, report)
            return
        _set(run, plan=plan, report=report, state=S.READY if report["ok"] else S.DESIGNING)
        _critic_says(run, report)
        if report["ok"]:
            return
        feedback = _feedback(report)
    _fail(run, f"Stopped after {max_rounds} rounds: the plan still has the problems listed above.")


# ---------------------------------------------------------------- Critic

def _problem_lines(report: dict) -> str:
    return "\n".join(f"- {p}" for p in report["problems"])


def _critic_says(run: ImportRun, report: dict) -> None:
    if report["ok"]:
        parts = []
        for name, t in report["tables"].items():
            parts.append(f"{name} -> {t['target']}: {t['new']} new rows, {t['exists']} already exist (skipped), "
                         f"{t['filtered']} removed by the filter, {t['skipped']} skipped by you, "
                         f"{len(t['maybe'])} may already exist (loaded unless you skip them)")
        text = "Passed. " + "; ".join(parts) + "."
        if report.get("dropped"):
            text += " Dropped columns: " + ", ".join(f"{d['sheet']}.{d['column']}" for d in report["dropped"]) + "."
        if report.get("warnings"):
            text += f" {len(report['warnings'])} rule warnings for the reviewer."
    elif report.get("nothing"):
        text = report["problems"][0]
    else:
        n = len(report["problems"])
        text = f"{n} problem{'s' if n != 1 else ''}:\n" + _problem_lines(report)
    _say(run, ImportMessage.Role.CRITIC, text, {"ok": report["ok"], "problems": report["problems"]})


def _add(problems: list, text: str) -> None:
    if len(problems) < MAX_PROBLEMS:
        problems.append(_clip(" ".join(str(text).split()), MAX_PROBLEM_CHARS))


def _both(report: dict, text: str, safe: str | None = None) -> None:
    """A problem for the person (`problems`) and its model-safe form (`feedback`; the same text by default)."""
    _add(report["problems"], text)
    _add(report["feedback"], text if safe is None else safe)


_QUOTED = re.compile(r"""'[^']*'|"[^"]*\"""")


def _scrub(text) -> str:
    """Quoted text (which may be a cell value) replaced by '...'."""
    return _QUOTED.sub("'...'", str(text))


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _problems(rep: dict, real) -> tuple[list[str], list[str]]:
    """(problems for the person, feedback for the model) from a validation report. The person's text is whole; the
    model's names the column, constraint or table and a count, never a cell value. `real` turns the script's
    stream names back into the sheet names and headers."""
    out = {"problems": [], "feedback": []}
    for e in rep.get("errors") or []:
        kind = e.get("kind")
        where = f"line {e['line']}: " if kind == "rows" and e.get("line") else (
            f"{e['statement']}: " if e.get("statement") and kind == "mapping" else "")
        person = (where or (f"{e['statement']}: " if e.get("statement") else "")) + e["message"]
        safe = where + (e["message"] if kind in ("mapping", "storage") else _scrub(e["message"]))
        _both(out, real(person), real(safe))
    gate = rep.get("gate") or {}
    for e in gate.get("errors") or []:
        _both(out, real(f"gate: {e}"))
    for v in gate.get("blocked") or []:
        _both(out, real(f"rule {v.get('rule', '?')}: {v.get('message', v)}" if isinstance(v, dict) else f"rule: {v}"))
    for t in rep.get("tables") or []:
        if t.get("orphans"):
            sample = (t.get("sample_orphans") or [{}])[0]
            parent, column = sample.get("parent", "parent"), sample.get("column")
            _both(out, real(f"{t['name']}: {t['orphans']} rows reference a missing {parent} "
                            f"(e.g. {column} = {sample.get('value')!r})"),
                  real(f"{t['name']}: {_plural(t['orphans'], 'row references', 'rows reference')} a missing "
                       f"{parent} (column {column})"))
    dry = rep.get("dry_run") or {}
    if dry.get("error"):
        _both(out, real(f"dry run: {dry['error']}"), real(f"dry run: {dry.get('safe_error') or 'it failed'}"))
    if not out["problems"] and not rep.get("ok"):
        _both(out, "the check did not pass")
    return out["problems"], out["feedback"]


_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _safe(name, used: set, fallback: str) -> str:
    """A name the script parser accepts (the gate's identifier rule); unchanged when it already does."""
    name = str(name)
    if IDENT_RE.fullmatch(name) and name not in used:
        out = name
    else:
        base = _UNSAFE.sub("_", name).strip("_-")[:60] or fallback
        if not re.match(r"[A-Za-z_]", base):
            base = f"c_{base}"[:60]
        out, n = base, 2
        while out in used:
            out, n = f"{base}_{n}", n + 1
    used.add(out)
    return out


def _real_names(tables: dict, columns: dict):
    """A function that rewrites stream names (`tables`: sheet -> stream, `columns`: sheet -> {header: stream}) in
    a text back to the sheet names and headers the person and the Designer know."""
    back_t = {st: sheet for sheet, st in tables.items() if st != sheet}

    def real(text: str) -> str:
        text = str(text)
        # columns of the sheets this text names come first: two sheets may share a stream column name
        named = [sheet for sheet, st in tables.items() if re.search(rf"(?<![\w-]){re.escape(st)}(?![\w-])", text)]
        back = {}
        for sheet in [*named, *(x for x in tables if x not in named)]:
            for h, sc in columns.get(sheet, {}).items():
                if sc != h:
                    back.setdefault(sc, h)
        back.update(back_t)
        if not back:
            return text
        pattern = re.compile(r"(?<![\w-])(" + "|".join(re.escape(k) for k in sorted(back, key=len, reverse=True))
                             + r")(?![\w-])")
        return pattern.sub(lambda m: back[m.group(1)], text)
    return real


def _as_streams(sources: list, profiles: list, plan: dict, schema: str) -> tuple[list, list, dict, object]:
    """Sheets and headers are what the person and the Designer see; the script needs parser-safe identifiers.
    Returns the sources, profiles and plan with sheet and header names replaced by their stream names (external
    ref parents qualified as `<schema>.<table>`, the way scripts.mapping names existing tables), and a function
    that turns stream names in a text back into sheet names and headers.

    Every name in the plan must already be a real sheet or header (`_unknown_names`): nothing passes through."""
    tables, columns, used_t = {}, {}, set()
    for i, s in enumerate(sources):
        tables[s["name"]] = _safe(s["name"], used_t, f"sheet_{i + 1}")
        used_c = set()
        columns[s["name"]] = {h: _safe(h, used_c, f"column_{j + 1}") for j, h in enumerate(s["headers"])}
    out_sources = [{**s, "name": tables[s["name"]], "headers": [columns[s["name"]][h] for h in s["headers"]]}
                   for s in sources]
    out_profiles = [{**p, "name": tables.get(p.get("name"), p.get("name"))} for p in profiles]

    def col(sheet, h):
        return columns[sheet][h] if isinstance(h, str) else h

    mapping = copy.deepcopy(plan.get("mapping") or {})
    if isinstance(mapping.get("tables"), dict):
        new = {}
        for sheet, entry in mapping["tables"].items():
            if isinstance(entry, dict):
                if isinstance(entry.get("columns"), dict):
                    entry["columns"] = {t: col(sheet, h) for t, h in entry["columns"].items()}
                if isinstance(entry.get("drop"), list):
                    entry["drop"] = [col(sheet, h) for h in entry["drop"]]
                for r in entry.get("refs") if isinstance(entry.get("refs"), list) else []:
                    if isinstance(r, dict):
                        r["source_column"] = col(sheet, r.get("source_column"))
                        if not r.get("external") and r.get("parent") in tables:
                            r["parent"] = tables[r["parent"]]
                        elif r.get("external") and isinstance(r.get("parent"), str) and "." not in r["parent"]:
                            r["parent"] = f"{schema}.{r['parent']}"
            new[tables[sheet]] = entry
        mapping["tables"] = new
    filters = {tables[k]: [{**c, "column": col(k, c.get("column"))} if isinstance(c, dict) else c
                           for c in v] if isinstance(v, list) else v
               for k, v in (plan.get("filters") or {}).items() if k in tables}
    skip = {tables[k]: v for k, v in (plan.get("skip") or {}).items() if k in tables}
    return out_sources, out_profiles, {**plan, "mapping": mapping, "filters": filters, "skip": skip}, \
        _real_names(tables, columns)


def _unknown_names(sources: list, mapping: dict, filters: dict) -> list[str]:
    """Sheets and headers the plan names that this file does not have (a stream name echoed back is one)."""
    out = []
    headers = {s["name"]: s["headers"] for s in sources}
    sheets = ", ".join(repr(n) for n in headers)
    for sheet, entry in ((mapping or {}).get("tables") or {}).items():
        if sheet not in headers:
            out.append(f"mapping.tables: unknown sheet {sheet!r} (the sheets are {sheets})")
            continue
        if not isinstance(entry, dict):
            continue
        hs = headers[sheet]
        used = list((entry.get("columns") or {}).values()) if isinstance(entry.get("columns"), dict) else []
        used += entry.get("drop") if isinstance(entry.get("drop"), list) else []
        for r in entry.get("refs") if isinstance(entry.get("refs"), list) else []:
            if isinstance(r, dict):
                used.append(r.get("source_column"))
                if not r.get("external") and isinstance(r.get("parent"), str) and r["parent"] not in headers:
                    out.append(f"{sheet}: ref parent {r['parent']!r} is not a sheet of this file; for an existing "
                               "table set \"external\": true")
        for h in dict.fromkeys(h for h in used if h is not None):
            if not isinstance(h, str) or h not in hs:
                out.append(f"{sheet}: unknown source column {h!r}; the headers are "
                           + ", ".join(repr(x) for x in hs))
    for sheet in (filters or {}):
        if sheet not in headers:
            out.append(f"filters: unknown sheet {sheet!r} (the sheets are {sheets})")
    return out


def _targets(mapping: dict) -> dict:
    return {n: e.get("target", n) for n, e in ((mapping or {}).get("tables") or {}).items() if isinstance(e, dict)}


def _row_check(run: ImportRun, src: dict, prof: dict, entry, state: dict) -> dict:
    """Which rows already exist (or may) in the plan's target, by the plan's own key mapping. The Matcher's check
    is reused only when it looked at the same table with the same header feeding the same key column."""
    target = entry.get("target", src["name"]) if isinstance(entry, dict) else None
    if target not in state:
        return {}
    cols = entry.get("columns") if isinstance(entry.get("columns"), dict) else {}
    cmap = {t: h for t, h in cols.items() if isinstance(h, str) and h in src["headers"]}
    rows = (((run.matches or {}).get("sources") or {}).get(src["name"]) or {}).get("rows") or {}
    if rows.get("table") == target and "key" in rows and \
            (rows.get("key"), rows.get("header")) == matcher.row_key(state, target, cmap):
        return rows
    if not get_config()["import"]["rowMatch"]:
        return {}
    return matcher.row_check(run.schema, src, {c["header"]: c for c in prof.get("columns") or []}, cmap,
                             target, state)


def _nothing_new(report: dict, mapping: dict, existing: set) -> bool:
    """True (and the report says so) when the plan would load no row and change no table."""
    tables = report["tables"]
    if any(t["new"] for t in tables.values()):
        return False
    for entry in ((mapping or {}).get("tables") or {}).values():
        if not isinstance(entry, dict):
            continue
        if entry.get("add_columns") or entry.get("target") not in existing:
            return False      # a schema change is something to submit, even with no rows
    exists = sum(t["exists"] for t in tables.values())
    filtered = sum(t["filtered"] for t in tables.values())
    skipped = sum(t["skipped"] for t in tables.values())
    parts = [p for n, p in ((exists, _plural(exists, "row already exists", "rows already exist")),
                            (filtered, f"{filtered} removed by the filter"),
                            (skipped, f"{skipped} skipped by you")) if n]
    text = "Nothing new to load: " + (", ".join(parts) if parts else "the file has no rows") + "."
    report["problems"], report["feedback"], report["nothing"] = [text], [text], True
    return True


def _critic(run: ImportRun, sources: list, profiles: list, plan: dict) -> dict:
    """Write the plan as a script, store it, and validate it. Never raises for a bad plan: it reports.

    Sets plan["skip"] to the effective skip (rows already existing in the target, plus the person's picks).
    report["problems"] is for the person; report["feedback"] is the same list as the model may see it."""
    mapping, filters = (plan or {}).get("mapping"), (plan or {}).get("filters") or {}
    picked = (plan or {}).get("picked") or {}
    report = {"ok": False, "problems": [], "feedback": [], "scriptId": None, "tables": {}, "badRows": [],
              "dropped": [], "warnings": []}
    for p in design.check_plan(mapping, filters):
        _both(report, p)
    if report["problems"]:
        return report
    for p in _unknown_names(sources, mapping, filters):
        _both(report, p)
    if report["problems"]:
        return report
    state = introspect.snapshot(run.schema)
    tables = (mapping or {}).get("tables") or {}
    by_name = {p.get("name"): p for p in profiles}
    skip = {}
    for s in sources:
        name = s["name"]
        try:
            kept = writer.apply_filter(s["rows"], s["headers"], filters.get(name))
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            _both(report, f"filters.{name}: {exc}")
            continue
        rows = _row_check(run, s, by_name.get(name) or {}, tables.get(name), state) if name in tables else {}
        exists = sorted({e["row"] for e in rows.get("exists") or []})
        mine = sorted(set(picked.get(name) or []) - set(exists))
        skip[name] = sorted(set(exists) | set(mine))
        kept1 = {n + 1 for n in kept}
        maybe = [m for m in rows.get("maybe") or [] if m["row"] in kept1 and m["row"] not in skip[name]]
        report["tables"][name] = {
            "target": _targets(mapping).get(name, name), "rows": len(s["rows"]),
            "filtered": len(s["rows"]) - len(kept1),
            "exists": len(kept1 & set(exists)), "skipped": len(kept1 & set(mine)),
            "new": len(kept1 - set(skip[name])), "maybe": maybe,
        }
    plan["skip"] = {k: v for k, v in skip.items() if v}     # the effective skip, kept on the plan
    if report["problems"]:
        return report
    if _nothing_new(report, mapping, set(state)):
        return report
    existing = sorted(state)
    try:
        s_sources, s_profiles, s_plan, real = _as_streams(sources, profiles,
                                                          {"mapping": mapping, "filters": filters, "skip": skip},
                                                          run.schema)
        data = writer.write(s_sources, s_profiles, {**s_plan, "existing": existing}, run.schema)
        script = storage.save(run.owner, _script_name(run.pk), data, kind="import")
    except (ValueError, TypeError, AttributeError, KeyError, ScriptError) as exc:
        _both(report, f"the plan could not be written as a script: {exc}",
              f"the plan could not be written as a script: {_scrub(exc)}")
        return report
    previous = (run.report or {}).get("scriptId")
    report["scriptId"] = script.pk
    rep = validate_mod.validate(script, s_plan["mapping"], run.schema)
    if previous and previous != script.pk:
        _drop_script(previous)
    report["ok"] = bool(rep.get("ok"))
    report["problems"], report["feedback"] = ([], []) if report["ok"] else _problems(rep, real)
    report["badRows"] = [{"line": e["line"], "message": _clip(real(e["message"]), MAX_PROBLEM_CHARS)}
                         for e in rep.get("errors") or [] if e.get("kind") == "rows" and e.get("line")][:MAX_PROBLEMS]
    report["warnings"] = [_clip(real(v.get("message", v) if isinstance(v, dict) else v), MAX_PROBLEM_CHARS)
                          for v in (rep.get("gate") or {}).get("warnings") or []][:MAX_PROBLEMS]
    reasons = (plan or {}).get("reasons") or {}
    for name, entry in ((mapping or {}).get("tables") or {}).items():
        for col in entry.get("drop") if isinstance(entry.get("drop"), list) else []:
            report["dropped"].append({"sheet": name, "column": col,
                                      "reason": reasons.get(f"{name}.{col}", "")})
    return report
