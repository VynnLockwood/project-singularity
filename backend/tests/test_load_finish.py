import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection

from accounts.models import Role
from approvals.models import Change
from scripts import loads, storage
from scripts.models import StagedLoad
from tests.mcp_helpers import applied, make_users

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]


@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    from config.siconfig import invalidate_overrides
    settings.SCRIPT_DIR = str(tmp_path / "scripts")
    invalidate_overrides()
    yield
    invalidate_overrides()


@pytest.fixture
def ctx():
    u = make_users()
    applied(u[Role.APPROVER], [
        {"op": "create_table", "name": "customer", "columns": [
            {"name": "code", "type": "text", "unique": True, "nullable": False},
            {"name": "name_en", "type": "text", "nullable": False}, {"name": "age", "type": "integer"}]},
        {"op": "create_table", "name": "province", "columns": [{"name": "code", "type": "text", "unique": True}]},
        {"op": "create_table", "name": "district", "columns": [
            {"name": "code", "type": "text", "unique": True}, {"name": "province_id", "type": "integer"}]},
        {"op": "insert_rows", "table": "province", "rows": [{"code": "10"}, {"code": "20"}]}])
    return u[Role.EDITOR], u[Role.APPROVER]


def started(ed, *chunks, table="customer", columns=("code", "name_en", "age"), **kw):
    lid = loads.begin(ed, table, list(columns), **kw)["load_id"]
    for i, c in enumerate(chunks, 1):
        out = loads.append(ed, lid, i, c)
        assert out["ok"], out
    return lid


def test_finish_makes_one_pending_change(ctx):
    ed, _ = ctx
    lid = started(ed, "c1,A,1\nc2,B,2\n", "c3,C,\n")
    out = loads.finish(ed, lid, "first load")
    assert out["ok"] and out["change_id"] and out["script_id"]
    ch = Change.objects.get(pk=out["change_id"])
    assert ch.status == "pending" and ch.source == "script"
    st = StagedLoad.objects.get(pk=lid)
    assert st.status == "finished" and st.path == "" and st.change_id == ch.pk


def test_finish_twice_returns_same_change(ctx):
    ed, _ = ctx
    lid = started(ed, "c1,A,1\n")
    first = loads.finish(ed, lid)
    again = loads.finish(ed, lid)
    assert again["change_id"] == first["change_id"] and again["ok"]
    assert Change.objects.filter(source="script").count() == 1


@pytest.mark.django_db(transaction=True)
def test_two_threads_finish_once(ctx):
    ed, _ = ctx
    lid = started(ed, "c1,A,1\n")

    def go(_):
        try:
            return loads.finish(ed, lid)
        finally:
            connection.close()
    with ThreadPoolExecutor(2) as ex:
        a, b = list(ex.map(go, range(2)))
    assert a["change_id"] == b["change_id"]
    assert Change.objects.filter(source="script").count() == 1


def test_finish_after_cancel_refuses(ctx):
    ed, _ = ctx
    lid = started(ed, "c1,A,1\n")
    loads.cancel(ed, lid)
    with pytest.raises(loads.LoadError, match="cancelled"):
        loads.finish(ed, lid)


def test_empty_load_refuses(ctx):
    ed, _ = ctx
    with pytest.raises(loads.LoadError, match="no rows"):
        loads.finish(ed, started(ed))


def test_table_dropped_after_begin_refuses(ctx):
    ed, appr = ctx
    lid = started(ed, "c1,A,1\n")
    from mcp_server import tools
    tools.approve_change(appr, tools.propose_change(appr, [{"op": "drop_table", "table": "customer",
                                                            "reason": "test"}])["id"])
    with pytest.raises(loads.LoadError, match="no longer exists"):
        loads.finish(ed, lid)


def test_duplicate_key_found_by_dry_run_reports_row_and_stays_open(ctx):
    ed, _ = ctx
    lid = started(ed, "c1,A,1\nc2,B,2\n", "c1,C,3\n")
    out = loads.finish(ed, lid)
    assert out["ok"] is False
    assert StagedLoad.objects.get(pk=lid).status == "open"
    text = str(out["report"])
    assert "line" not in " ".join(k for e in out["report"]["errors"] for k in e)
    assert Change.objects.filter(source="script").count() == 0, text


def test_external_ref_orphan_reports_load_row(ctx):
    ed, _ = ctx
    ref = {"column": "province_id", "parent_table": "province", "parent_key": "code", "source_column": "province_code"}
    lid = started(ed, "d1,10\nd2,99\nd3,20\n", table="district", columns=("code", "province_code"), refs=[ref])
    out = loads.finish(ed, lid)
    assert out["ok"] is False
    (t,) = [t for t in out["report"]["tables"] if t["name"] == "district"]
    assert t["orphans"] == 1
    (s,) = t["sample_orphans"]
    assert s["row"] == 2 and s["value"] == "99" and "line" not in s


def test_external_ref_load_in_master_submits(ctx):
    ed, _ = ctx
    ref = {"column": "province_id", "parent_table": "province", "parent_key": "code", "source_column": "province_code"}
    lid = started(ed, "d1,10\nd2,20\n", table="district", columns=("code", "province_code"), refs=[ref])
    out = loads.finish(ed, lid)
    assert out["ok"] and out["change_id"], out


def test_finish_in_non_master_database(ctx):
    ed, appr = ctx
    from mcp_server import tools
    tools.approve_change(appr, tools.propose_change(appr, [{"op": "create_schema", "name": "crm"}])["id"])
    applied(appr, [{"op": "create_table", "name": "thing", "columns": [{"name": "code", "type": "text", "unique": True}]},
                   {"op": "create_table", "name": "part", "columns": [{"name": "code", "type": "text", "unique": True},
                                                                      {"name": "thing_id", "type": "integer"}]},
                   {"op": "insert_rows", "table": "thing", "rows": [{"code": "t1"}]}], schema="crm")
    ref = {"column": "thing_id", "parent_table": "thing", "parent_key": "code", "source_column": "thing_code"}
    lid = loads.begin(ed, "part", ["code", "thing_code"], schema="crm", refs=[ref])["load_id"]
    loads.append(ed, lid, 1, "p1,t1\np2,t1\n")
    out = loads.finish(ed, lid)
    assert out["ok"], out
    assert Change.objects.get(pk=out["change_id"]).schema == "crm"
