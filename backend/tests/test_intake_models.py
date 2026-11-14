import hashlib
import io
import os

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from config.siconfig import get_config
from scripts import storage, uploads
from scripts.models import UploadSession
from scripts.storage import ScriptError

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="imp", password="x")


def _xlsx_bytes():
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(["a", "b"])
    wb.active.append([1, 2])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_import_defaults():
    assert get_config()["import"] == {"model": "", "maxFileBytes": 52428800, "sampleValues": 5,
                                      "maxRounds": 3, "rowMatch": True}


def test_begin_import_rules(user):
    with pytest.raises(ScriptError):
        uploads.begin(user, "x.pdf", 10, kind="import")
    with pytest.raises(ScriptError):
        uploads.begin(user, "x.xlsx", 52428801, kind="import")
    out = uploads.begin(user, "x.xlsx", 10, kind="import")
    assert out["max_bytes"] == 52428800
    assert UploadSession.objects.get(pk=out["upload_id"]).kind == "import"
    assert uploads.begin(user, "y.CSV", 10, kind="import")["upload_id"]
    # script kind default unchanged
    assert UploadSession.objects.get(pk=uploads.begin(user, "s.sql", 10)["upload_id"]).kind == "script"


def test_complete_raw_keeps_bytes(user):
    data = _xlsx_bytes()
    b = uploads.begin(user, "book.xlsx", len(data), kind="import")
    uploads.put_chunk(user, b["upload_id"], 0, data)
    path = uploads.complete_raw(user, b["upload_id"])
    assert os.path.dirname(path) == storage.imports_dir() and path.endswith(".xlsx")
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == hashlib.sha256(data).hexdigest()
    s = UploadSession.objects.get(pk=b["upload_id"])
    assert s.status == "complete"
    assert not os.path.exists(os.path.join(storage.uploads_dir(), s.path or "none"))


def test_complete_raw_refuses_missing_chunks(user):
    b = uploads.begin(user, "a.csv", uploads.CHUNK_BYTES + 5, kind="import")
    uploads.put_chunk(user, b["upload_id"], 1, b"x" * 5)
    with pytest.raises(ScriptError, match="Chunks missing: 0"):
        uploads.complete_raw(user, b["upload_id"])


def test_complete_raw_refuses_script_kind(user):
    b = uploads.begin(user, "a.sql", 3)
    uploads.put_chunk(user, b["upload_id"], 0, b"abc")
    with pytest.raises(ScriptError):
        uploads.complete_raw(user, b["upload_id"])


def test_complete_refuses_import_kind(user):
    b = uploads.begin(user, "a.csv", 3, kind="import")
    uploads.put_chunk(user, b["upload_id"], 0, b"a,b")
    with pytest.raises(ScriptError):
        uploads.complete(user, b["upload_id"])


def test_models_save_and_recipe_unique(user):
    from intake.models import ImportMessage, ImportRecipe, ImportRun
    run = ImportRun.objects.create(owner=user, schema="master", prompt="p", file_path="/x", file_name="x.csv",
                                   expires_at="2030-01-01T00:00:00Z")
    run.refresh_from_db()
    assert run.state == "profiling" and run.round == 0 and run.plan == {} and run.heartbeat is None
    ImportMessage.objects.create(run=run, role="profiler", text="hi")
    assert run.messages.count() == 1
    ImportRecipe.objects.create(schema="master", fingerprint="a" * 64, created_by=user)
    with pytest.raises(IntegrityError), transaction.atomic():
        ImportRecipe.objects.create(schema="master", fingerprint="a" * 64, created_by=user)
    ImportRecipe.objects.create(schema="other", fingerprint="a" * 64, created_by=user)
