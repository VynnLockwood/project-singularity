"""Task 10: REST endpoints for script intake (scripts/api.py)."""
import json
import os

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from accounts.models import Role, User

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]

MINI_PATH = os.path.join(os.path.dirname(__file__), "data", "mini_dump.sql")
with open(MINI_PATH, encoding="utf-8") as _f:
    MINI = _f.read()


@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")
    return settings.SCRIPT_DIR


def login(role):
    u, _ = User.objects.get_or_create(username=f"{role.name.lower()}@example.com", email=f"{role.name.lower()}@example.com", role=role)
    c = Client()
    c.force_login(u)
    return c


@pytest.fixture
def editor():
    return login(Role.EDITOR)


@pytest.fixture
def viewer():
    return login(Role.VIEWER)


@pytest.fixture
def approver():
    return login(Role.APPROVER)


def post_json(c, url, data=None):
    import json as _json
    return c.post(url, data=_json.dumps(data or {}), content_type="application/json")


def upload(c, content=MINI, name="mini.sql"):
    return c.post("/api/scripts", {"file": SimpleUploadedFile(name, content.encode("utf-8"))})


def mapping(report):
    """Reuse the mapping stored at validation time so submit is consistent."""
    from scripts.models import Script
    return Script.objects.get(pk=report["script_id"]).mapping


# ---- auth and roles --------------------------------------------------------

def test_401_anonymous():
    assert Client().post("/api/scripts", {}).status_code == 401
    assert Client().get("/api/scripts").status_code == 401


def test_403_viewer_cannot_upload_validate_or_submit(viewer):
    assert viewer.post("/api/scripts", {"file": SimpleUploadedFile("m.sql", MINI.encode())}).status_code == 403
    assert post_json(viewer, "/api/scripts/1/validate").status_code == 403
    assert post_json(viewer, "/api/scripts/1/submit").status_code == 403


def test_viewer_reads_ok(viewer):
    assert viewer.get("/api/scripts").status_code == 200


# ---- upload ----------------------------------------------------------------

def test_upload_ok(editor):
    r = upload(editor)
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["script_id"] > 0 and body["size"] == len(MINI.encode()) and len(body["sha256"]) == 64


def test_upload_requires_file_field(editor):
    assert editor.post("/api/scripts", {}).status_code == 400


def test_upload_over_limit_is_413(editor):
    from config.siconfig import get_config
    from settings_app import tunables
    a = User.objects.create(username="a413@example.com", email="a413@example.com", role=Role.APPROVER)
    tunables.apply(a, {"limits.maxScriptBytes": 1048576})
    get_config.cache_clear()
    r = upload(editor, "x" * 2_000_000)
    assert r.status_code == 413, r.content
    tunables.apply(a, {"limits.maxScriptBytes": 52428800})
    get_config.cache_clear()


# ---- validate / submit round trip ------------------------------------------

def test_validate_then_submit_as_change(editor, approver):
    sid = upload(editor).json()["script_id"]
    r = post_json(editor, f"/api/scripts/{sid}/validate")
    assert r.status_code == 200, r.content
    report = r.json()
    assert report["ok"], report["errors"]
    r = post_json(editor, f"/api/scripts/{sid}/submit")
    assert r.status_code == 201, r.content
    change_id = r.json()["change_id"]
    from approvals.models import Change
    change = Change.objects.get(pk=change_id)
    assert change.status == Change.Status.PENDING and change.source == "script"
    # submitter (editor) cannot approve own script change
    r = post_json(editor, f"/api/changes/{change_id}/approve")
    assert r.status_code == 403


def test_submit_without_validate_is_409(editor):
    sid = upload(editor).json()["script_id"]
    r = post_json(editor, f"/api/scripts/{sid}/submit")
    assert r.status_code == 409


# ---- read ------------------------------------------------------------------

def test_detail_owner_or_approver(editor, approver, viewer):
    sid = upload(editor, "COPY t (a) FROM stdin;\n1\tx\n\\.\n", "small.sql").json()["script_id"]
    assert editor.get(f"/api/scripts/{sid}").status_code == 200
    assert viewer.get(f"/api/scripts/{sid}").status_code == 403
    assert approver.get(f"/api/scripts/{sid}").status_code == 200


def test_detail_404(editor):
    assert editor.get("/api/scripts/9999").status_code == 404


def test_list_shows_own_only(editor, approver):
    from scripts.models import Script
    Script.objects.all().delete()
    a = upload(editor, "COPY t (a) FROM stdin;\n1\tx\n\\.\n").json()["script_id"]
    listed = editor.get("/api/scripts").json()["scripts"]
    assert [s["id"] for s in listed] == [a]
    other_u, _ = User.objects.get_or_create(username="editor2@example.com", email="editor2@example.com", role=Role.EDITOR)
    other = Client()
    other.force_login(other_u)
    b = upload(other, "COPY t (a) FROM stdin;\n1\tx\n\\.\n").json()["script_id"]
    assert [s["id"] for s in editor.get("/api/scripts").json()["scripts"]] == [a]
    assert {s["id"] for s in approver.get("/api/scripts").json()["scripts"]} == {a, b}


def test_paste_json_upload(editor):
    payload = json.dumps({"name": "p.sql", "content": "CREATE TABLE p_t (id integer);\n"})
    r = editor.post("/api/scripts", data=payload, content_type="application/json")
    assert r.status_code == 201, (r.status_code, r.content[:200])
    assert "script_id" in r.json()


def test_paste_rejects_empty_content(editor):
    r = editor.post("/api/scripts", data='{"name": "p.sql", "content": "  "}',
                    content_type="application/json")
    assert r.status_code == 400 and "content" in r.json()["error"]


def test_paste_rejected_for_viewer(viewer):
    r = viewer.post("/api/scripts", data='{"name": "p.sql", "content": "SELECT 1;"}',
                    content_type="application/json")
    assert r.status_code == 403
