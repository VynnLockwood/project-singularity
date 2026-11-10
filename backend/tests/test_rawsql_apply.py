"""Raw SQL: approve, apply, stale, recheck (plan C task 5)."""
from pathlib import Path

import pytest
from django.conf import settings

from accounts.models import Role, User
from approvals import service
from audit.models import AuditLog
from schema import executor, introspect
from schema.backup import BackupError
from settings_app import tunables

from .ops_helpers import apply, col, make_table, rows

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]
REASON = "change things for a test"


@pytest.fixture(autouse=True)
def fresh_settings():
    from config.siconfig import invalidate_overrides
    invalidate_overrides()
    yield
    invalidate_overrides()


def user(role, name=None):
    n = name or role
    return User.objects.create(username=f"{n}@x.com", email=f"{n}@x.com", role=role)


@pytest.fixture
def editor():
    return user(Role.EDITOR)


@pytest.fixture
def approver():
    return user(Role.APPROVER)


def cols():
    return introspect.snapshot("master")["t"]["columns"]


def test_apply_runs_all_statements_and_audits(editor, approver):
    make_table(rows=[{"a": "1"}, {"a": "2"}])
    ch = service.propose_sql(editor, "ALTER TABLE master.t ADD COLUMN c int; UPDATE master.t SET c = 7", REASON)
    ch = service.approve(approver, ch.pk)
    assert ch.status == "applied", ch.error
    assert ch.applied_at and ch.reverse_operations is None and ch.decided_by == approver
    assert "c" in cols() and {r[0] for r in rows("SELECT c FROM master.t")} == {7}
    acts = list(AuditLog.objects.values_list("action", flat=True))
    assert {"change.approved", "backup.created", "change.applied"} <= set(acts)
    applied = AuditLog.objects.get(action="change.applied")
    assert applied.details["sql"].startswith("ALTER TABLE") and applied.details["backup"].endswith(".dump")
    assert applied.details["reversible"] is False
    assert any(f"change-{ch.pk}" in p.name for p in Path(settings.BACKUP_DIR).iterdir())


def test_failure_in_the_third_of_four_statements_changes_nothing(editor, approver, monkeypatch):
    make_table(rows=[{"a": "1"}])
    sql = ("ALTER TABLE master.t ADD COLUMN c int; UPDATE master.t SET c = 1; "
           "SELECT pg_sleep(0.6); UPDATE master.t SET c = 2")
    ch = service.propose_sql(editor, sql, REASON)
    monkeypatch.setattr(executor, "STATEMENT_TIMEOUT", "200ms")          # statement 3 now runs out of time
    ch = service.approve(approver, ch.pk)
    assert ch.status == "failed" and "statement 3" in ch.error, (ch.status, ch.error)
    assert "c" not in cols()
    assert rows("SELECT count(*) FROM master.t")[0][0] == 1


def test_data_that_moved_since_the_dry_run_makes_the_change_stale(editor, approver):
    make_table(extra=[col("a"), col("flag")], rows=[{"a": "1", "flag": "n"}, {"a": "2", "flag": "n"}])
    ch = service.propose_sql(editor, "DELETE FROM master.t WHERE flag = 'y'", REASON)
    assert ch.diff == ["DELETE 0 (statement 1)"]
    apply([{"op": "update_rows", "table": "t", "where": {"a": "1"}, "set": {"flag": "y"}}])
    ch = service.approve(approver, ch.pk)
    assert ch.status == "stale" and "data changed" in ch.error, (ch.status, ch.error)
    assert rows("SELECT count(*) FROM master.t")[0][0] == 2


def test_recheck_is_refused_when_raw_sql_is_switched_off(editor, approver):
    make_table()
    ch = service.propose_sql(editor, "UPDATE master.t SET a = 'x'", REASON)
    tunables.apply(approver, {"sql.enabled": False})
    with pytest.raises(service.ChangeError, match="switched off"):
        service.recheck(editor, ch.pk)


def test_a_refused_proposal_is_audited_with_its_sql(editor):
    make_table()
    with pytest.raises(service.ChangeError):
        service.propose_sql(editor, "UPDATE master.t SET a = 1; SELECT * FROM nope", REASON)
    e = AuditLog.objects.get(action="change.proposal_failed")
    assert e.details["sql"].startswith("UPDATE master.t") and "nope" in e.details["error"]
    assert e.details["schema"] == "master"


def test_proposed_audit_carries_the_sql(editor):
    make_table()
    service.propose_sql(editor, "UPDATE master.t SET a = 'x'", REASON)
    assert AuditLog.objects.get(action="change.proposed").details["sql"] == "UPDATE master.t SET a = 'x'"


def test_backup_failure_runs_nothing(editor, approver, monkeypatch):
    make_table()
    ch = service.propose_sql(editor, "ALTER TABLE master.t ADD COLUMN c int", REASON)

    def boom(*a, **k):
        raise BackupError("disk full")
    monkeypatch.setattr(service, "backup_schema", boom)
    ch = service.approve(approver, ch.pk)
    assert ch.status == "failed" and ch.error.startswith("backup failed, nothing was changed")
    assert "c" not in cols()


def test_stale_when_the_schema_moved_then_recheck_and_apply(editor, approver):
    make_table()
    ch = service.propose_sql(editor, "UPDATE master.t SET a = 'x'", REASON)
    old = ch.fingerprint
    apply([{"op": "add_column", "table": "t", "column": col("b")}])
    ch = service.approve(approver, ch.pk)
    assert ch.status == "stale"
    ch = service.recheck(editor, ch.pk)
    assert ch.status == "pending" and ch.fingerprint != old and ch.diff
    assert service.approve(approver, ch.pk).status == "applied"


def test_recheck_that_now_fails_is_rejected(editor, approver):
    make_table()
    ch = service.propose_sql(editor, "UPDATE master.t SET a = 'x'", REASON)
    apply([{"op": "drop_table", "table": "t", "reason": "test"}])
    ch = service.approve(approver, ch.pk)
    assert ch.status == "stale"
    ch = service.recheck(approver, ch.pk)
    assert ch.status == "rejected" and ch.check_result["errors"]


def test_editor_cannot_approve_and_switch_off_refuses(editor, approver):
    make_table()
    ch = service.propose_sql(editor, "ALTER TABLE master.t ADD COLUMN c int", REASON)
    with pytest.raises(service.ChangeError) as e:
        service.approve(editor, ch.pk)
    assert e.value.code == "forbidden"
    tunables.apply(approver, {"sql.enabled": False})
    with pytest.raises(service.ChangeError, match="switched off") as e:
        service.approve(approver, ch.pk)
    assert e.value.code == "bad_state"
    assert "c" not in cols()


def test_applied_sql_change_reindexes_search(editor, approver, monkeypatch, django_capture_on_commit_callbacks):
    from search import indexer
    called = []
    monkeypatch.setattr(indexer, "schedule_reindex", lambda: called.append(1))
    make_table()
    ch = service.propose_sql(editor, "ALTER TABLE master.t ADD COLUMN c int", REASON)
    with django_capture_on_commit_callbacks(execute=True):
        service.approve(approver, ch.pk)
    assert called
