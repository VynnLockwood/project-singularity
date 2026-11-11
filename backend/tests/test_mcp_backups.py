"""create_backup and list_backups: approver-only, confined to BACKUP_DIR."""
import os
import pathlib
import shutil

import pytest

from accounts.models import Role
from audit.models import AuditLog
from mcp_server import tools
from schema import backup

from .mcp_helpers import make_users

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]

needs_pg_dump = pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump is not installed")


@pytest.fixture
def users():
    return make_users()


def touch(folder, name, size=3):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"x" * size)


@needs_pg_dump
def test_create_backup_writes_a_dump_and_audits(users, settings):
    out = tools.create_backup(users[Role.APPROVER])
    folder = pathlib.Path(settings.BACKUP_DIR)
    assert out["schema"] == "master" and out["bytes"] > 0 and out["created_at"].endswith("Z")
    assert out["file"].startswith("master-") and out["file"].endswith("-manual.dump")
    assert (folder / out["file"]).is_file()
    row = AuditLog.objects.get(action="backup.created")
    assert row.target == out["file"]


def test_create_backup_uses_the_configured_retention(users, monkeypatch, settings):
    seen = {}

    def fake(schema, label, retention_days):
        seen.update(schema=schema, label=label, days=retention_days)
        p = pathlib.Path(settings.BACKUP_DIR) / f"{schema}-20260101T000000Z-{label}.dump"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"abc")
        return p

    monkeypatch.setattr(backup, "backup_schema", fake)
    from config.siconfig import get_config
    days = get_config()["backups"]["retentionDays"]
    out = tools.create_backup(users[Role.APPROVER], label="before-import")
    assert seen == {"schema": "master", "label": "before-import", "days": days}
    assert out["bytes"] == 3 and out["file"] == "master-20260101T000000Z-before-import.dump"


def test_backups_are_approver_tools(users):
    for name in ("create_backup", "list_backups"):
        assert tools.TOOL_ROLES[name] == "approver"
        assert tools.allowed(users[Role.APPROVER], name)
        assert not tools.allowed(users[Role.EDITOR], name)
        assert not tools.allowed(users[Role.VIEWER], name)


@pytest.mark.parametrize("kwargs", [
    {"label": "../x"}, {"label": "a/b"}, {"label": ""}, {"label": "a b"},
    {"schema": "master; drop schema master"}, {"schema": "Master"}, {"schema": "../etc"}, {"schema": "nope"},
])
def test_hostile_label_or_schema_is_refused_and_writes_nothing(users, settings, kwargs):
    with pytest.raises(tools.ToolFailure):
        tools.create_backup(users[Role.APPROVER], **kwargs)
    folder = pathlib.Path(settings.BACKUP_DIR)
    assert not folder.exists() or list(folder.iterdir()) == []
    assert not AuditLog.objects.filter(action="backup.created").exists()


def test_backup_error_becomes_a_tool_failure(users, monkeypatch):
    def boom(schema, label, retention_days):
        raise backup.BackupError("pg_dump is not installed")

    monkeypatch.setattr(backup, "backup_schema", boom)
    with pytest.raises(tools.ToolFailure, match="pg_dump is not installed"):
        tools.create_backup(users[Role.APPROVER])
    assert not AuditLog.objects.filter(action="backup.created").exists()


def test_list_backups_newest_first_filtered_and_only_real_dumps(users, settings):
    folder = pathlib.Path(settings.BACKUP_DIR)
    touch(folder, "master-20260101T000000Z-manual.dump", 10)
    touch(folder, "master-20260102T000000Z-change-5.dump", 20)
    touch(folder, "other-20260103T000000Z-x.dump", 30)
    touch(folder, "notes.txt")
    touch(folder, "bad.dump")
    (folder / "sub-20260104T000000Z-dir.dump").mkdir()            # a folder is not a backup
    out = tools.list_backups(users[Role.APPROVER])["backups"]
    assert [b["file"] for b in out] == [
        "other-20260103T000000Z-x.dump", "master-20260102T000000Z-change-5.dump", "master-20260101T000000Z-manual.dump"]
    assert out[1] == {"file": "master-20260102T000000Z-change-5.dump", "schema": "master", "label": "change-5",
                      "bytes": 20, "created_at": "2026-01-02T00:00:00Z"}
    only = tools.list_backups(users[Role.APPROVER], schema="master")["backups"]
    assert [b["label"] for b in only] == ["change-5", "manual"]


def test_list_backups_with_no_folder_is_empty(users, settings):
    assert not os.path.exists(settings.BACKUP_DIR)
    assert tools.list_backups(users[Role.APPROVER]) == {"backups": []}
