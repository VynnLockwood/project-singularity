"""Task 7: a script from browser to applied change over the real views, and the 250 MB dump in a real child process."""
import json
import os
import resource
import subprocess
import sys
import time

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import Client

from accounts.models import Role, User
from schema.conn import ro_connection
from scripts import jobs, uploads
from tests import bigdump

JSON = "application/json"
BIN = "application/octet-stream"


@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")


def client(role, n):
    u = User.objects.get_or_create(username=f"{n}@x.io", defaults={"email": f"{n}@x.io", "role": role})[0]
    c = Client()
    c.force_login(u)
    return c


def upload(c, data: bytes, name="d.sql") -> int:
    r = c.post("/api/scripts/uploads", json.dumps({"name": name, "size": len(data)}), content_type=JSON)
    assert r.status_code == 201, r.content
    info = r.json()
    for n in range(info["chunks"]):
        part = data[n * info["chunk_bytes"]:(n + 1) * info["chunk_bytes"]]
        assert c.put(f"/api/scripts/uploads/{info['upload_id']}/{n}", part, content_type=BIN).status_code == 200
    r = c.post(f"/api/scripts/uploads/{info['upload_id']}/complete", "{}", content_type=JSON)
    assert r.status_code == 201, r.content
    return r.json()["script_id"]


def poll(c, script_id, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        rep = c.get(f"/api/scripts/{script_id}").json()["report"] or {}
        if rep.get("state") in ("done", "failed"):
            return rep
        time.sleep(2)
    raise AssertionError("the check did not finish in time")


@pytest.mark.django_db
@pytest.mark.usefixtures("master")
def test_chunked_upload_validate_submit_approve_roundtrip(monkeypatch):
    monkeypatch.setattr(uploads, "CHUNK_BYTES", 1024)
    real = subprocess.Popen     # jobs.subprocess is the subprocess module: other callers (backups) keep the real one

    def popen(argv, **kw):
        if "validate_script" in argv:
            return call_command("validate_script", argv[-1])
        return real(argv, **kw)
    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    ed, ap = client(Role.EDITOR, "ed"), client(Role.APPROVER, "ap")
    sid = upload(ed, bigdump.PROBE.encode())
    r = ed.post(f"/api/scripts/{sid}/validate?async=1", "{}", content_type=JSON)
    assert r.status_code == 202
    rep = poll(ed, sid, 30)
    assert rep["state"] == "done" and rep["ok"], rep
    assert {"extension", "sequence", "default", "permission", "logic"} <= {s["kind"] for s in rep["skipped"]}
    r = ed.post(f"/api/scripts/{sid}/submit", json.dumps({"note": "e2e"}), content_type=JSON)
    assert r.status_code == 201
    done = ap.post(f"/api/changes/{r.json()['change_id']}/approve", "{}", content_type=JSON)
    assert done.status_code == 200 and done.json()["status"] == "applied", done.content
    with ro_connection() as conn:
        # the app gives every table its own identity key; the script's key survives as the business key `code`
        rows = [(r["code"], r["name_th"]) for r in conn.execute("SELECT code, name_th FROM master.province")]
    assert rows == [(10, "กรุงเทพมหานคร")]


@pytest.mark.slow
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("master")
def test_250mb_chunked_end_to_end(monkeypatch, tmp_path, master, settings):
    info = bigdump.write_dump(tmp_path / "big.sql")
    assert info["tables"] == 80 and info["rows"] == 192_000 and info["bytes"] > 240 * 2**20
    peak = tmp_path / "peak"
    shim = ("import resource, subprocess, sys; r = subprocess.run(sys.argv[2:]); "
            "open(sys.argv[1], 'w').write(str(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss))")
    real = subprocess.Popen

    def child(argv, **kw):    # a real child process, pointed at the test database, whose peak memory we read
        if "validate_script" not in argv:
            return real(argv, **kw)
        kw["env"] = {**os.environ, "DJANGO_SETTINGS_MODULE": "tests.e2e_settings",
                     "E2E_APP_DB": connection.settings_dict["NAME"], "E2E_MASTER_DB": master,
                     "SCRIPT_DIR": settings.SCRIPT_DIR, "BACKUP_DIR": settings.BACKUP_DIR}
        return real([sys.executable, "-c", shim, str(peak), *argv], **kw)
    monkeypatch.setattr(jobs.subprocess, "Popen", child)

    monkeypatch.setattr("scripts.storage.get_config", lambda: {"limits": {"maxScriptBytes": 536870912}})
    ed = client(Role.EDITOR, "big")
    with open(tmp_path / "big.sql", "rb") as f:
        data = f.read()
    sid = upload(ed, data)
    del data
    assert ed.post(f"/api/scripts/{sid}/validate?async=1", "{}", content_type=JSON).status_code == 202
    rep = poll(ed, sid, 20 * 60)
    assert rep["state"] == "done" and rep["ok"], {k: rep.get(k) for k in ("message", "errors", "gate", "dry_run")}
    assert len(rep["tables"]) == 80 and sum(t["rows"] for t in rep["tables"]) == 192_000
    time.sleep(1)
    rss_mib = int(peak.read_text()) / 1024          # ru_maxrss is KiB on Linux
    print(f"\nE2E child_peak_rss_mib={rss_mib:.0f}")
    assert rss_mib < RSS_LIMIT_MB


# Peak resident memory of the child process (measured 734 MiB, +25%). Resident memory includes the interpreter and
# Django, so it sits above the Python-heap bound of test_script_scale.py (637 measured, 800 allowed).
RSS_LIMIT_MB = 920
