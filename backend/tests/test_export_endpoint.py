"""GET /api/schema/export: parameters, role rule, row cap, errors, streaming and audit."""
import datetime
import json

import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import StreamingHttpResponse
from django.test import RequestFactory

from accounts.models import Role, User
from audit.models import AuditLog
from config.siconfig import get_config
from schema import conn, views
from settings_app import tunables

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]
RF = RequestFactory()


def build(*statements):
    with conn.rw_connection(autocommit=True) as c:
        for s in statements:
            c.execute(s)


def seed():
    build(
        "CREATE TABLE master.province (id integer PRIMARY KEY, name text)",
        "INSERT INTO master.province VALUES (1, 'a'), (2, 'b'), (3, 'c')",
    )


def user(role):
    return User.objects.create(username=f"{role}@x.com", email=f"{role}@x.com", role=role)


def call(who, query=""):
    req = RF.get("/api/schema/export" + query)
    req.user = who
    return views.export(req)


def jb(resp):
    return json.loads(resp.content)


def body(resp):
    return b"".join(resp.streaming_content).decode()


@pytest.fixture(autouse=True)
def opened(master, monkeypatch):
    """Every Export a test opens; closed at teardown so a failing test cannot block dropping the database."""
    made = []
    real = views.open_export

    def spy(*a, **k):
        ex = real(*a, **k)
        made.append(ex)
        return ex

    monkeypatch.setattr(views, "open_export", spy)
    yield made
    for ex in made:
        ex.close()


def exports():
    return AuditLog.objects.filter(action="schema.export")


def test_viewer_gets_structure_but_403_for_data():
    seed()
    v = user(Role.VIEWER)
    r = call(v, "?data=0")
    assert r.status_code == 200 and r["Content-Type"].startswith("text/sql")
    body(r)
    r = call(v, "?data=1")
    assert r.status_code == 403 and r["Content-Type"].startswith("application/json")
    assert "error" in jb(r)
    r = call(user(Role.EDITOR), "?data=1")
    assert r.status_code == 200
    body(r)


def test_unauthenticated_401():
    assert call(AnonymousUser()).status_code == 401


def test_headers_and_filename():
    seed()
    r = call(user(Role.VIEWER))
    body(r)
    assert r["Content-Type"].startswith("text/sql")
    stamp = datetime.date.today().strftime("%Y%m%d")
    assert r["Content-Disposition"] == f'attachment; filename="singularity-master-{stamp}.sql"'


def test_default_params_are_master_and_no_data():
    seed()
    out = body(call(user(Role.VIEWER)))
    assert 'CREATE TABLE "master"."province"' in out and "COPY " not in out


def test_bad_schema_is_400_and_bad_data_flag_is_400():
    v = user(Role.EDITOR)
    for q in ('?schema=nope', '?schema=master"; DROP TABLE x;--', "?schema=", "?data=2", "?data=yes", "?data="):
        r = call(v, q)
        assert r.status_code == 400, q
        assert "error" in jb(r)
    assert exports().count() == 0


def test_over_cap_is_413_json_naming_table():
    seed()
    tunables.apply(user(Role.APPROVER), {"limits.maxExportRows": 1000})
    build("CREATE TABLE master.big (id integer)", "INSERT INTO master.big SELECT g FROM generate_series(1, 1500) g")
    r = call(user(Role.EDITOR), "?data=1")
    assert r.status_code == 413
    assert not isinstance(r, StreamingHttpResponse)
    assert r["Content-Type"].startswith("application/json")
    assert "big" in jb(r)["error"] and "CREATE TABLE" not in r.content.decode()
    assert exports().count() == 0


def test_sequence_default_is_422_json():
    build("CREATE TABLE master.t (id serial)")  # owned sequence goes away with the table
    r = call(user(Role.VIEWER))
    assert r.status_code == 422 and "sequence" in jb(r)["error"]
    assert exports().count() == 0


def test_audit_event_recorded():
    seed()
    call(user(Role.VIEWER), "?data=1")  # denied: nothing recorded
    assert exports().count() == 0
    r = call(user(Role.EDITOR), "?data=1")
    body(r)
    ev = exports().get()
    assert ev.actor_email == "editor@x.com"
    assert ev.details["schema"] == "master" and ev.details["data"] is True
    assert ev.details["counts"] == {"province": 3}


def test_streaming_response_is_not_buffered():
    seed()
    r = call(user(Role.EDITOR), "?data=1")
    assert isinstance(r, StreamingHttpResponse)
    out = body(r)
    assert 'COPY "master"."province"' in out and out.rstrip().endswith(("\\.", ";"))


def test_limit_tunable_validates_range():
    get_config.cache_clear()
    assert get_config()["limits"]["maxExportRows"] == 1000000
    a = user(Role.APPROVER)
    for bad in (999, 20000001):
        with pytest.raises(tunables.TunableError):
            tunables.apply(a, {"limits.maxExportRows": bad})
    tunables.apply(a, {"limits.maxExportRows": 20000000})
    assert get_config()["limits"]["maxExportRows"] == 20000000


# --- ASGI streaming -------------------------------------------------------------------------

import warnings  # noqa: E402

from asgiref.sync import async_to_sync  # noqa: E402
from django.test import AsyncRequestFactory  # noqa: E402

ARF = AsyncRequestFactory()


def acall(who, query=""):
    req = ARF.get("/api/schema/export" + query)
    req.user = who
    return views.export(req)


async def _collect(resp):
    return [part async for part in resp.streaming_content]


def test_asgi_response_has_async_iterator_and_same_body():
    seed()
    ed = user(Role.EDITOR)
    expected = body(call(ed, "?data=1"))
    resp = acall(ed, "?data=1")
    assert resp.is_async and hasattr(resp.streaming_content, "__aiter__")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        parts = async_to_sync(_collect)(resp)
    assert len(parts) > 1  # chunk by chunk, not one buffered blob
    assert b"".join(parts).decode() == expected


def test_asgi_does_not_buffer_synchronous_iterator_via_django_aiter():
    seed()
    resp = acall(user(Role.EDITOR), "?data=1")

    async def consume():
        async for _ in resp:  # what Django's ASGI handler does
            pass

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # "must consume synchronous iterators" would raise
        async_to_sync(consume)()


def test_asgi_early_aclose_closes_connection(opened):
    seed()
    resp = acall(user(Role.EDITOR), "?data=1")
    ex = opened[-1]
    assert not ex.conn.closed

    async def first_then_close():
        it = resp._iterator  # the object handed to the response; Django's streaming_content wraps it
        await it.__anext__()
        await it.aclose()

    async_to_sync(first_then_close)()
    assert ex.conn.closed


def test_asgi_response_close_closes_connection(opened):
    seed()
    resp = acall(user(Role.EDITOR), "?data=1")
    resp.close()
    assert opened[-1].conn.closed


def test_asgi_runs_next_in_one_thread():
    # the psycopg connection must only ever be used from one thread
    seed()
    import threading
    resp = acall(user(Role.EDITOR), "?data=1")
    seen = set()
    chunks = resp._iterator._chunks
    orig = chunks.__class__.__next__

    def spy(self):
        seen.add(threading.get_ident())
        return orig(self)

    chunks.__class__.__next__ = spy
    try:
        async_to_sync(_collect)(resp)
    finally:
        chunks.__class__.__next__ = orig
    assert len(seen) == 1


def test_no_store_cache_control():
    seed()
    r = call(user(Role.VIEWER))
    body(r)
    assert "no-store" in r["Cache-Control"]


def test_asyncclient_end_to_end_streams_asynchronously():
    from django.test import AsyncClient

    seed()
    ed = user(Role.EDITOR)
    expected = body(call(ed, "?data=1"))

    async def run():
        client = AsyncClient()
        await client.aforce_login(ed)
        resp = await client.get("/api/schema/export?data=1")
        assert resp.status_code == 200 and resp.is_async
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            return b"".join([p async for p in resp.streaming_content]).decode()

    assert async_to_sync(run)() == expected
