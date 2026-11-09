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
