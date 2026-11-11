"""Task 2: the validation report lists skipped statements; a file with nothing to import says so."""
import pytest

from accounts.models import Role, User
from scripts import storage
from scripts.validate import MAX_IGNORED, validate
from tests.bigdump import PROBE

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")


@pytest.fixture
def owner():
    return User.objects.create(username="sk@x.io", email="sk@x.io", role=Role.EDITOR)


@pytest.mark.usefixtures("master")
def test_report_lists_skipped_and_is_ok(owner):
    r = validate(storage.save(owner, "p.sql", PROBE.encode()), None)
    assert r["ok"], r
    assert r["dry_run"]["ok"] is True
    kinds = {s["kind"] for s in r["skipped"]}
    assert {"extension", "sequence", "default", "permission", "logic"} <= kinds
    assert all({"line", "statement", "reason", "kind"} <= set(s) for s in r["skipped"])
    assert r["skipped_truncated"] == 0


@pytest.mark.usefixtures("master")
def test_only_skipped_is_nothing_to_import(owner):
    r = validate(storage.save(owner, "n.sql", b"CREATE EXTENSION pg_trgm;\nGRANT ALL ON SCHEMA public TO app;\n"), None)
    assert r["ok"] is False
    assert [e["message"] for e in r["errors"]] == ["Nothing to import: the file has no tables or rows."]
    assert len(r["skipped"]) == 2


@pytest.mark.usefixtures("master")
def test_skipped_list_is_capped(owner):
    sql = "CREATE TABLE master.a (id integer);\n" + "GRANT ALL ON TABLE master.a TO app;\n" * (MAX_IGNORED + 5)
    r = validate(storage.save(owner, "c.sql", sql.encode()), None)
    assert len(r["skipped"]) == MAX_IGNORED and r["skipped_truncated"] == 5
