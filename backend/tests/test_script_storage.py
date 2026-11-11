import hashlib
import os
import stat
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import Role, User
from approvals.models import Change
from scripts import storage
from scripts.models import Script
from scripts.storage import ScriptError

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")
    return settings.SCRIPT_DIR


def small_limit(monkeypatch, n=10):
    monkeypatch.setattr(storage, "get_config", lambda: {"limits": {"maxScriptBytes": n}})


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


def test_oversize_rejected(owner, monkeypatch):
    small_limit(monkeypatch)
    storage.save(owner, "ok.sql", b"x" * 10)
    with pytest.raises(ScriptError):
        storage.save(owner, "a.sql", b"x" * 11)
    assert Script.objects.count() == 1


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
    a = storage.save(owner, "a.sql", b"select 1")
    b = storage.save(owner, "b.sql", b"select 2")
    assert storage.purge_expired(now=timezone.now() + timedelta(days=7, seconds=1)) == 2
    for s in (a, b):
        s.refresh_from_db()
        assert s.status == "expired" and s.path == ""
        with pytest.raises(ScriptError):
            storage.read(s)
    assert os.listdir(script_dir) == []
    assert storage.purge_expired(now=timezone.now() + timedelta(days=30)) == 0


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


def test_purge_rechecks_under_lock(owner, script_dir, monkeypatch):
    s = storage.save(owner, "a.sql", b"select 1")
    later = timezone.now() + timedelta(days=8)
    original = storage._candidate_ids

    def listing_then_submit(now):
        ids = original(now)
        change = Change.objects.create(requester=owner, source="web", kind="data", status="pending")
        Script.objects.filter(pk=s.pk).update(change=change, status="submitted")
        return ids

    monkeypatch.setattr(storage, "_candidate_ids", listing_then_submit)
    assert storage.purge_expired(now=later) == 0
    s.refresh_from_db()
    assert s.status == "submitted" and s.path
    assert storage.read(s) == b"select 1"


def test_purge_does_not_overwrite_concurrent_expiry(owner, script_dir):
    s = storage.save(owner, "a.sql", b"select 1")
    Script.objects.filter(pk=s.pk).update(status="expired", path="")
    assert storage.purge_expired(now=timezone.now() + timedelta(days=8)) == 0


THAI = "SELECT 'สวัสดี ก็ กิ ก้ ก\u0e4d\u0e33 น้ำ';\n-- ภาษาไทย\n"


def test_thai_round_trip(owner):
    raw = ("\ufeff" + THAI.replace("\n", "\r\n")).encode("utf-8")
    s = storage.save(owner, "ไทย.sql", raw)
    assert storage.read(s) == THAI.encode("utf-8")
    assert s.name == "ไทย.sql"


@pytest.mark.parametrize("data", [b"", b" \n", b"\xff\xfe", b"select 1;\x00", b"x" * 11])
def test_rejected_inputs_leave_nothing(owner, script_dir, monkeypatch, data):
    small_limit(monkeypatch)
    with pytest.raises(ScriptError):
        storage.save(owner, "a.sql", data)
    assert Script.objects.count() == 0
    assert not os.path.exists(script_dir) or os.listdir(script_dir) == []


def test_write_failure_leaves_no_file(owner, script_dir, monkeypatch):
    def boom(fd):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        storage.save(owner, "a.sql", b"select 1")
    assert os.listdir(script_dir) == []
    assert Script.objects.count() == 0


def test_db_failure_leaves_no_file(owner, script_dir, monkeypatch):
    def boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(Script.objects, "create", boom)
    with pytest.raises(RuntimeError):
        storage.save(owner, "a.sql", b"select 1")
    assert os.listdir(script_dir) == []


def test_directory_made_private_even_if_preexisting(owner, script_dir):
    os.makedirs(script_dir, mode=0o755)
    os.chmod(script_dir, 0o755)
    storage.save(owner, "a.sql", b"select 1")
    assert stat.S_IMODE(os.stat(script_dir).st_mode) == 0o700


def test_stored_path_must_be_bare_filename(owner):
    s = storage.save(owner, "a.sql", b"select 1")
    for bad in ("../x.sql", "a/b.sql", "/etc/passwd"):
        s.path = bad
        with pytest.raises(ScriptError):
            storage.read(s)


@pytest.mark.parametrize("name", [".", "..", "", "a/..", "\x00"])
def test_odd_names_fall_back(owner, name):
    assert storage.save(owner, name, b"select 1").name == "script.sql"


def test_read_text_matches_read_and_checks_the_checksum(tmp_path, settings):
    from accounts.models import Role, User
    from scripts import storage
    settings.SCRIPT_DIR = str(tmp_path / "s")
    u = User.objects.create(username="rt@x.io", email="rt@x.io", role=Role.EDITOR)
    text = "SELECT 1;\n" + "กรุงเทพ " * 1_000_000 + "\n"           # several 8 MiB blocks, multi-byte characters
    s = storage.save(u, "t.sql", text.encode())
    assert storage.read_text(s) == storage.read(s).decode() == text
    with open(storage._full_path(s.path), "ab") as f:
        f.write(b"-- tampered\n")
    with pytest.raises(storage.ScriptError, match="checksum"):
        storage.read_text(s)
