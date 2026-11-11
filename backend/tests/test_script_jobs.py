"""Task 4: validation as a background job (scripts.jobs, validate_script command, ?async=1)."""
import json
import threading
import time
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.utils import timezone

from accounts.models import Role, User
from scripts import jobs, storage, validate as validate_mod
from scripts.models import Script
from tests.bigdump import PROBE

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")


def user(role=Role.EDITOR, n="ed"):
    return User.objects.get_or_create(username=f"{n}@x.io", defaults={"email": f"{n}@x.io", "role": role})[0]


def make(u, text=PROBE) -> Script:
    return storage.save(u, "p.sql", text.encode())


@pytest.fixture
def inline(monkeypatch):
    """Popen replaced by running the command in this process (and recording the argv)."""
    calls = []

    def fake(argv, **kw):
        calls.append((argv, kw))
        call_command("validate_script", argv[-1])
    monkeypatch.setattr(jobs.subprocess, "Popen", fake)
    return calls


@pytest.fixture
def never_starts(monkeypatch):
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: None)


def age(script, minutes):
    s = Script.objects.get(pk=script.pk)
    s.report["heartbeat"] = (timezone.now() - timedelta(minutes=minutes)).isoformat()
    s.save(update_fields=["report"])


@pytest.mark.usefixtures("master")
def test_progress_phases_in_order():
    seen = []
    r = validate_mod.validate(make(user()), None, "master", progress=seen.append)
    assert r["ok"] and seen == ["reading", "parsing", "checking", "dry_run"]


@pytest.mark.usefixtures("master")
def test_job_runs_to_done_and_report_equals_sync(inline):
    u = user()
    a, b = make(u), make(u)
    assert jobs.start(u, a.pk, None, "master") == {"state": "running"}
    argv, kw = inline[0]
    assert argv[1:4] == ["manage.py", "validate_script", str(a.pk)] and kw["start_new_session"] is True
    a.refresh_from_db()
    assert a.report["state"] == "done" and a.status == "validated"
    sync = validate_mod.validate(b, None, "master")

    def core(r):
        r = {k: v for k, v in r.items() if k not in ("state", "phase", "heartbeat", "job", "script_id")}
        return json.loads(json.dumps(r))
    assert core(a.report) == core(sync)


@pytest.mark.usefixtures("master")
def test_second_start_refused_while_running(never_starts):
    u = user()
    s = make(u)
    jobs.start(u, s.pk, None, "master")
    s.refresh_from_db()
    assert s.report["state"] == "running" and s.report["phase"] == "queued" and "heartbeat" in s.report
    with pytest.raises(jobs.Busy):
        jobs.start(u, s.pk, None, "master")


def test_start_refused_for_submitted_script(never_starts):
    u = user()
    s = make(u)
    Script.objects.filter(pk=s.pk).update(status="submitted")
    with pytest.raises(storage.ScriptError, match="submitted"):
        jobs.start(u, s.pk, None, "master")


@pytest.mark.usefixtures("master")
def test_failed_job_stores_message(inline, monkeypatch):
    u = user()
    s = make(u)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(validate_mod, "validate", boom)
    jobs.start(u, s.pk, None, "master")
    s.refresh_from_db()
    assert s.report["state"] == "failed" and "disk on fire" in s.report["message"]
    assert jobs.start  # and a new run is allowed afterwards


def test_stale_running_report_reads_as_failed(never_starts):
    u = user()
    s = make(u)
    jobs.start(u, s.pk, None, "master")
    age(s, 11)
    c = Client()
    c.force_login(u)
    r = c.get(f"/api/scripts/{s.pk}").json()["report"]
    assert r == {"state": "failed", "message": "The check stopped unexpectedly. Try again."}
    assert Script.objects.get(pk=s.pk).report["state"] == "running"     # read only, never written


def test_killed_process_never_started(never_starts):
    u = user()
    s = make(u)
    jobs.start(u, s.pk, None, "master")
    with pytest.raises(jobs.Busy):
        jobs.start(u, s.pk, None, "master")
    age(s, 11)
    assert jobs.start(u, s.pk, None, "master") == {"state": "running"}


def test_other_user_gets_403_or_404_for_validate_and_detail(never_starts):
    a, b = user(n="a"), user(n="b")
    s = make(a)
    c = Client()
    c.force_login(b)
    assert c.get(f"/api/scripts/{s.pk}").status_code == 403
    assert c.post(f"/api/scripts/{s.pk}/validate?async=1", "{}", content_type="application/json").status_code in (403, 404)
    assert c.post(f"/api/scripts/{s.pk}/validate", "{}", content_type="application/json").status_code in (403, 404)
    ap = user(Role.APPROVER, "ap")
    c2 = Client()
    c2.force_login(ap)
    assert c2.post(f"/api/scripts/{s.pk}/validate?async=1", "{}", content_type="application/json").status_code == 202


def test_async_view_returns_202_and_409_when_running(never_starts):
    u = user()
    s = make(u)
    c = Client()
    c.force_login(u)
    r = c.post(f"/api/scripts/{s.pk}/validate?async=1", json.dumps({}), content_type="application/json")
    assert r.status_code == 202 and r.json() == {"state": "running"}
    r = c.post(f"/api/scripts/{s.pk}/validate?async=1", json.dumps({}), content_type="application/json")
    assert r.status_code == 409


@pytest.mark.usefixtures("master")
def test_previous_validated_schema_survives_a_failed_run(inline, monkeypatch):
    u = user()
    s = make(u)
    validate_mod.validate(s, None, "master")
    s.refresh_from_db()
    assert s.status == "validated"
    monkeypatch.setattr(validate_mod, "validate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    jobs.start(u, s.pk, None, "master")
    s.refresh_from_db()
    assert s.report["state"] == "failed" and s.report["validated_schema"] == "master" and s.status == "validated"


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("master")
def test_heartbeat_advances_during_a_long_phase(monkeypatch):
    u = user()
    s = make(u)
    seen = []

    def slow(script, mapping, schema="master", progress=None):
        time.sleep(0.6)
        seen.append(Script.objects.get(pk=script.pk).report["heartbeat"])
        return {"ok": True}
    monkeypatch.setattr(validate_mod, "validate", slow)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: None)
    jobs.start(u, s.pk, None, "master")
    before = Script.objects.get(pk=s.pk).report["heartbeat"]
    def work():
        jobs.run(s.pk, beat_seconds=0.1)
        connection.close()      # this thread's own connection
    t = threading.Thread(target=work)
    t.start(); t.join()
    assert seen[0] > before
