"""Task 9: submit script as a Change (scripts.service) and its apply with row_source."""
import os

import pytest

from accounts.models import Role, User
from approvals import service as approvals
from approvals.models import Change
from schema import introspect, query
from scripts import service, storage
from scripts.mapping import suggest_mapping
from scripts.models import Script
from scripts.sqlparse import parse_script

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]

MINI_PATH = os.path.join(os.path.dirname(__file__), "data", "mini_dump.sql")
with open(MINI_PATH, encoding="utf-8") as _f:
    MINI = _f.read()

CUSTOMER = [{"op": "create_table", "name": "customer",
             "columns": [{"name": "customer_code", "type": "text", "unique": True}, {"name": "name", "type": "text"}]}]

PROVINCE = [{"op": "create_table", "name": "province",
             "columns": [{"name": "province_id", "type": "integer"}, {"name": "province_name", "type": "text"}]}]

DATA_SCRIPT = (
    "COPY province (province_id, province_name) FROM stdin;\n"
    "10\tAlpha\n"
    "11\tBeta\n"
    "\\.\n"
)


@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")
    return settings.SCRIPT_DIR


def user(email, role):
    return User.objects.create(username=email, email=email, role=role)


@pytest.fixture
def editor():
    return user("editor@example.com", Role.EDITOR)


@pytest.fixture
def approver():
    return user("approver@example.com", Role.APPROVER)


@pytest.fixture
def viewer():
    return user("viewer@example.com", Role.VIEWER)


def validated(editor, text=DATA_SCRIPT, mapping=None):
    s = service.upload(editor, "dump.sql", text.encode("utf-8"))
    service.validate(editor, s.pk, mapping)
    s.refresh_from_db()
    return s


# ---------------------------------------------------------------- upload

def test_viewer_cannot_upload(viewer, editor):
    with pytest.raises(PermissionError):
        service.upload(viewer, "x.sql", b"COPY t (a) FROM stdin;\n1\n\\.\n")
    assert Script.objects.count() == 0
    assert service.upload(editor, "x.sql", b"COPY t (a) FROM stdin;\n1\n\\.\n").status == Script.Status.UPLOADED


# ---------------------------------------------------------------- submit a data-only script

def test_editor_submit_data_only_then_self_approve_loads_rows(editor, approver):
    _c = approvals.propose(approver, PROVINCE, source="web")
    approvals.approve(approver, _c.pk)
    s = validated(editor)
    change = service.submit(editor, s.pk, note="load provinces")
    assert change.source == "script" and change.kind == "data" and change.status == Change.Status.PENDING
    s.refresh_from_db()
    assert s.status == Script.Status.SUBMITTED and s.change_id == change.pk

    applied = approvals.approve(editor, change.pk)   # editors decide their own data change
    assert applied.status == Change.Status.APPLIED
    rows = query.run_select('SELECT province_id, province_name FROM province ORDER BY 1')["rows"]
    assert rows == [[10, "Alpha"], [11, "Beta"]]


# ---------------------------------------------------------------- submit a schema-containing script

def test_create_table_script_waits_for_an_approver(editor, approver):
    s = service.upload(editor, "mini.sql", MINI.encode("utf-8"))
    report = service.validate(editor, s.pk, suggest_mapping(parse_script(MINI)))
    change = service.submit(editor, s.pk)
    assert change.kind == "schema" and change.status == Change.Status.PENDING
    with pytest.raises(approvals.ChangeError) as e:
        approvals.approve(editor, change.pk)
    assert e.value.code == "forbidden"
    applied = approvals.approve(approver, change.pk)
    assert applied.status == Change.Status.APPLIED
    rows = query.run_select("SELECT code, province_name FROM province ORDER BY 1")["rows"]
    assert rows == [["10", "กรุงเทพมหานคร"], ["11", "สมุทรปราการ"]]
    assert query.run_select("SELECT count(*) FROM district")["rows"][0][0] == 2
    assert query.run_select("SELECT count(*) FROM subdistrict")["rows"][0][0] == 3
    s.refresh_from_db()
    assert s.status == Script.Status.SUBMITTED


# ---------------------------------------------------------------- preconditions

def test_second_submit_is_refused(editor, approver):
    _c = approvals.propose(approver, PROVINCE, source="web")
    approvals.approve(approver, _c.pk)
    s = validated(editor)
    service.submit(editor, s.pk)
    with pytest.raises(storage.ScriptError):
        service.submit(editor, s.pk)


def test_tampered_file_is_refused(editor, approver):
    from django.conf import settings as dj
    _c = approvals.propose(approver, PROVINCE, source="web")
    approvals.approve(approver, _c.pk)
    s = validated(editor)
    with open(os.path.join(dj.SCRIPT_DIR, s.path), "ab") as f:
        f.write(b"tamper\n")
    with pytest.raises(storage.ScriptError):
        service.submit(editor, s.pk)


def test_expired_script_is_refused(editor, approver):
    _c = approvals.propose(approver, PROVINCE, source="web")
    approvals.approve(approver, _c.pk)
    s = validated(editor)
    Script.objects.filter(pk=s.pk).update(expires_at="2020-01-01T00:00:00Z")
    with pytest.raises(storage.ScriptError):
        service.submit(editor, s.pk)
    s.refresh_from_db()
