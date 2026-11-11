"""Task 3: chunked upload streamed to disk (scripts.uploads, storage.save_stream, the four endpoints)."""
import hashlib
import json
import os
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from accounts.models import Role, User
from scripts import storage, uploads
from scripts.models import Script, UploadSession

pytestmark = pytest.mark.django_db
CH = 4096


@pytest.fixture(autouse=True)
def setup(settings, tmp_path, monkeypatch):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")
    monkeypatch.setattr(uploads, "CHUNK_BYTES", CH)


def user(role=Role.EDITOR, n="ed"):
    return User.objects.get_or_create(username=f"{n}@x.io", defaults={"email": f"{n}@x.io", "role": role})[0]


def send(u, data: bytes, name="d.sql"):
    info = uploads.begin(u, name, len(data))
    assert info["chunk_bytes"] == CH and info["chunks"] == -(-len(data) // CH)
    for n in range(info["chunks"]):
        uploads.put_chunk(u, info["upload_id"], n, data[n * CH:(n + 1) * CH])
    return info, uploads.complete(u, info["upload_id"])


def stored(s) -> bytes:
    with open(storage._full_path(s.path), "rb") as f:
        return f.read()


def single(u, data: bytes):
    return storage.save(u, "x.sql", data)


def test_chunks_equal_single_upload():
    u = user()
    data = ("SELECT 1;\r\n" * 1200 + "กรุงเทพ\r\n" * 1000).encode()          # ~30 KB, many chunk boundaries
    assert len(data) > 7 * CH
    _, s = send(u, data)
    ref = single(u, data)
    assert (s.sha256, s.size) == (ref.sha256, ref.size) and stored(s) == stored(ref)
    assert s.status == Script.Status.UPLOADED and s.name == "d.sql"


def test_crlf_pair_straddling_a_chunk_boundary():
    u = user()
    data = b"a" * (CH - 1) + b"\r\nb\r\n" + b"c" * 10
    _, s = send(u, data)
    assert stored(s) == stored(single(u, data)) and b"\r" not in stored(s)


def test_multibyte_char_straddling_a_chunk_boundary():
    u = user()
    data = b"a" * (CH - 1) + "ก".encode() + b"z" * 10     # first byte of the char ends chunk 0
    _, s = send(u, data)
    assert stored(s) == stored(single(u, data))


def test_bom_and_lone_cr_match_single_path():
    u = user()
    data = b"\xef\xbb\xbf" + b"x\rcreate\r\ny" + b"\r" * 3 + b"end"
    _, s = send(u, data)
    assert stored(s) == stored(single(u, data)) and not stored(s).startswith(b"\xef")


def test_out_of_order_and_resent_chunk():
    u = user()
    data = os.urandom(0).join([b"line %d\n" % i for i in range(1500)])
    info = uploads.begin(u, "o.sql", len(data))
    uid, n = info["upload_id"], info["chunks"]
    order = list(range(n))[::-1]
    for k in order:
        uploads.put_chunk(u, uid, k, data[k * CH:(k + 1) * CH])
    uploads.put_chunk(u, uid, 0, data[:CH])                      # resend overwrites
    assert uploads.status(u, uid)["received"] == list(range(n))
    assert stored(uploads.complete(u, uid)) == stored(single(u, data)) and hashlib.sha256(data).hexdigest()


def test_short_last_chunk_and_wrong_length_refused():
    u = user()
    data = b"x" * (2 * CH + 100)
    info = uploads.begin(u, "s.sql", len(data))
    uid = info["upload_id"]
    with pytest.raises(storage.ScriptError, match="4096"):
        uploads.put_chunk(u, uid, 0, data[:CH - 1])
    with pytest.raises(storage.ScriptError, match="100"):
        uploads.put_chunk(u, uid, 2, data[2 * CH:2 * CH + 99])
    with pytest.raises(storage.ScriptError):
        uploads.put_chunk(u, uid, 3, b"x")
    uploads.put_chunk(u, uid, 2, data[2 * CH:])
    assert uploads.status(u, uid)["received"] == [2]


def test_missing_chunk_named_at_complete():
    u = user()
    data = b"x\n" * (2 * CH)
    info = uploads.begin(u, "m.sql", len(data))
    uploads.put_chunk(u, info["upload_id"], 0, data[:CH])
    uploads.put_chunk(u, info["upload_id"], 3, data[3 * CH:4 * CH])
    with pytest.raises(storage.ScriptError, match=r"1, 2"):
        uploads.complete(u, info["upload_id"])
    assert UploadSession.objects.get(pk=info["upload_id"]).status == "open"


def test_over_cap_refused_at_begin_with_numbers(monkeypatch):
    monkeypatch.setattr(storage, "get_config", lambda: {"limits": {"maxScriptBytes": 1000}})
    monkeypatch.setattr(uploads, "get_config", lambda: {"limits": {"maxScriptBytes": 1000}})
    u = user()
    with pytest.raises(storage.ScriptError, match=r"1001.*1000|1000.*1001"):
        uploads.begin(u, "b.sql", 1001)
    with pytest.raises(storage.ScriptError):
        uploads.begin(u, "b.sql", 0)


def test_nul_and_invalid_utf8_refused_at_complete():
    u = user()
    for bad, why in ((b"abc\x00def", "NUL"), (b"abc\xff\xfedef", "UTF-8")):
        info = uploads.begin(u, "bad.sql", len(bad))
        uploads.put_chunk(u, info["upload_id"], 0, bad)
        with pytest.raises(storage.ScriptError, match=why):
            uploads.complete(u, info["upload_id"])
    assert Script.objects.count() == 0


def test_other_users_session_is_404_and_viewer_403():
    a, b, v = user(n="a"), user(n="b"), user(Role.VIEWER, "v")
    ca, cb, cv = Client(), Client(), Client()
    ca.force_login(a), cb.force_login(b), cv.force_login(v)
    r = ca.post("/api/scripts/uploads", json.dumps({"name": "x.sql", "size": 10}), content_type="application/json")
    uid = r.json()["upload_id"]
    assert cb.get(f"/api/scripts/uploads/{uid}").status_code == 404
    assert cb.put(f"/api/scripts/uploads/{uid}/0", b"x" * 10, content_type="application/octet-stream").status_code == 404
    assert cb.post(f"/api/scripts/uploads/{uid}/complete", "{}", content_type="application/json").status_code == 404
    assert cv.post("/api/scripts/uploads", json.dumps({"name": "x.sql", "size": 10}),
                   content_type="application/json").status_code == 403


def test_expired_session_is_410_and_purged():
    u = user()
    c = Client()
    c.force_login(u)
    info = uploads.begin(u, "e.sql", 10)
    uploads.put_chunk(u, info["upload_id"], 0, b"select 1;\n")
    part = os.path.join(storage.uploads_dir(), os.listdir(storage.uploads_dir())[0])
    UploadSession.objects.filter(pk=info["upload_id"]).update(expires_at=timezone.now() - timedelta(minutes=1))
    assert c.get(f"/api/scripts/uploads/{info['upload_id']}").status_code == 410
    assert uploads.purge_expired() == 1 and not os.path.exists(part)
    assert not UploadSession.objects.filter(pk=info["upload_id"]).exists()


def test_completed_session_is_410():
    u = user()
    info, _ = send(u, b"select 1;\n")
    c = Client()
    c.force_login(u)
    assert c.get(f"/api/scripts/uploads/{info['upload_id']}").status_code == 410


def test_http_flow():
    u = user()
    c = Client()
    c.force_login(u)
    data = b"select 1;\n" * 1000
    r = c.post("/api/scripts/uploads", json.dumps({"name": "h.sql", "size": len(data)}), content_type="application/json")
    assert r.status_code == 201
    info = r.json()
    assert {"upload_id", "chunk_bytes", "chunks", "max_bytes"} <= set(info) and info["chunk_bytes"] == CH
    uid = info["upload_id"]
    for n in range(info["chunks"]):
        r = c.put(f"/api/scripts/uploads/{uid}/{n}", data[n * CH:(n + 1) * CH], content_type="application/octet-stream")
        assert r.status_code == 200 and n in r.json()["received"]
    r = c.get(f"/api/scripts/uploads/{uid}")
    assert r.json() == {"received": list(range(info["chunks"])), "size": len(data), "chunks": info["chunks"]}
    r = c.post(f"/api/scripts/uploads/{uid}/complete", "{}", content_type="application/json")
    assert r.status_code == 201
    out = r.json()
    assert out["size"] == len(data) and out["sha256"] == hashlib.sha256(data).hexdigest()
    assert Script.objects.get(pk=out["script_id"]).owner == u
    assert c.put(f"/api/scripts/uploads/{uid}/0", b"x", content_type="application/octet-stream").status_code == 410
    bad = c.post("/api/scripts/uploads", json.dumps({"name": "h.sql", "size": 0}), content_type="application/json")
    assert bad.status_code == 400


def test_limits_endpoint():
    c = Client()
    c.force_login(user())
    r = c.get("/api/scripts/limits")
    assert r.status_code == 200
    assert r.json() == {"max_bytes": 536870912, "chunk_bytes": CH}
    v = Client()
    v.force_login(user(Role.VIEWER, "vv"))
    assert v.get("/api/scripts/limits").status_code == 403


def test_refused_file_leaves_no_part_and_session_is_closed():          # review finding 1
    u = user()
    info = uploads.begin(u, "bad.sql", 7)
    uploads.put_chunk(u, info["upload_id"], 0, b"abc\x00def")
    with pytest.raises(storage.ScriptError, match="NUL"):
        uploads.complete(u, info["upload_id"])
    assert os.listdir(storage.uploads_dir()) == []
    assert UploadSession.objects.get(pk=info["upload_id"]).status != "open"


def test_open_sessions_are_capped_per_user_and_begin_purges_expired():
    u = user()
    first = [uploads.begin(u, "a.sql", 10)["upload_id"] for _ in range(uploads.MAX_OPEN)]
    with pytest.raises(storage.ScriptError, match="open uploads"):
        uploads.begin(u, "a.sql", 10)
    UploadSession.objects.filter(pk__in=first).update(expires_at=timezone.now() - timedelta(minutes=1))
    uploads.begin(u, "a.sql", 10)                                       # the expired ones were purged first
    assert UploadSession.objects.filter(pk__in=first).count() == 0
