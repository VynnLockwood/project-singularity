import hashlib
import os
import stat
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import Role, User
from approvals.models import Change
from config import siconfig
from scripts import storage
from scripts.models import Script
from scripts.storage import ScriptError

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")
    return settings.SCRIPT_DIR


@pytest.fixture
def owner():
    return User.objects.create(username="o@x.io", email="o@x.io", role=Role.EDITOR)


def test_save_hashes_stored_bytes(owner, script_dir):
    s = storage.save(owner, "a.sql", b"select 1;\n")
    assert s.sha256 == hashlib.sha256(b"select 1;\n").hexdigest()
    assert s.size == 10 and s.status == "uploaded" and s.owner == owner
    assert storage.read(s) == b"select 1;\n"
    assert s.expires_at - s.created_at == storage.SCRIPT_TTL == timedelta(days=7)
    assert os.path.dirname(os.path.join(script_dir, s.path)) == script_dir


def test_empty_and_whitespace_rejected(owner):
    for data in (b"", b" \n\t\r\n", b"\xef\xbb\xbf  \n"):
        with pytest.raises(ScriptError):
            storage.save(owner, "a.sql", data)
    assert Script.objects.count() == 0


def test_oversize_rejected(owner, settings):
    max_bytes = siconfig.get_config()["limits"]["maxScriptBytes"]
    with pytest.raises(ScriptError):
        storage.save(owner, "a.sql", b"x" * (max_bytes + 1))


def test_not_utf8_rejected(owner):
    with pytest.raises(ScriptError):
        storage.save(owner, "a.sql", b"select '\xff\xfe';")


def test_bom_and_crlf_normalized(owner):
    s = storage.save(owner, "a.sql", b"\xef\xbb\xbfa\r\nb\rc\n")
    assert storage.read(s) == b"a\nb\nc\n"
    assert s.sha256 == hashlib.sha256(b"a\nb\nc\n").hexdigest()


def test_permissions_and_random_name(owner, script_dir):
    s = storage.save(owner, "../../etc/evil name.sql", b"select 1")
    assert stat.S_IMODE(os.stat(script_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(os.path.join(script_dir, s.path)).st_mode) == 0o600
    assert "evil" not in s.path and "/" not in s.path
    assert os.listdir(script_dir) == [s.path]
    assert s.name == "evil name.sql"


def test_long_name_truncated(owner):
    assert len(storage.save(owner, "n" * 500, b"select 1").name) == 200


def test_read_detects_tampering(owner, script_dir):
    s = storage.save(owner, "a.sql", b"select 1")
    with open(os.path.join(script_dir, s.path), "wb") as f:
        f.write(b"drop table x")
    with pytest.raises(ScriptError):
        storage.read(s)


def test_read_missing_or_expired(owner, script_dir):
    s = storage.save(owner, "a.sql", b"select 1")
    os.unlink(os.path.join(script_dir, s.path))
    with pytest.raises(ScriptError):
        storage.read(s)
    s.status = "expired"
    with pytest.raises(ScriptError):
        storage.read(s)


def test_purge_expired_removes_file(owner, script_dir):
    old = storage.save(owner, "a.sql", b"select 1")
    fresh = storage.save(owner, "b.sql", b"select 2")
    n = storage.purge_expired(now=timezone.now() + timedelta(days=7, seconds=1))
    assert n == 2
    old.refresh_from_db()
    assert old.status == "expired"
    assert os.listdir(script_dir) == []
    with pytest.raises(ScriptError):
        storage.read(old)
    assert storage.purge_expired(now=timezone.now() + timedelta(days=30)) == 0
    fresh.refresh_from_db()
    assert fresh.status == "expired"


def test_purge_keeps_unexpired(owner, script_dir):
    s = storage.save(owner, "a.sql", b"select 1")
    assert storage.purge_expired() == 0
    assert os.listdir(script_dir) == [s.path]


def test_purge_skips_pending_change(owner, script_dir):
    s = storage.save(owner, "a.sql", b"select 1")
    change = Change.objects.create(requester=owner, source="web", kind="data", status="pending")
    s.change = change
    s.status = "submitted"
    s.save()
    later = timezone.now() + timedelta(days=8)
    assert storage.purge_expired(now=later) == 0
    assert storage.read(s) == b"select 1"
    change.status = "applied"
    change.save()
    assert storage.purge_expired(now=later) == 1
    s.refresh_from_db()
    assert s.status == "expired"
