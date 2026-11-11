"""Final-review findings on staged loads: each test failed before its fix."""
import os
import threading
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import Role
from approvals.models import Change
from scripts import loads, storage
from scripts.models import Script, StagedLoad
from settings_app import tunables
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
            {"name": "desc", "type": "text"}, {"name": "end", "type": "text"}]},
        {"op": "create_table", "name": "province", "columns": [{"name": "code", "type": "text", "unique": True},
                                                                {"name": "label", "type": "text"}]},
        {"op": "create_table", "name": "district", "columns": [{"name": "code", "type": "text", "unique": True},
                                                                {"name": "province_id", "type": "integer"}]}])
    return u[Role.EDITOR], u[Role.APPROVER]


REF = {"column": "province_id", "parent_table": "province", "parent_key": "code", "source_column": "province_code"}


def test_keyword_column_names_load(ctx):                       # finding 1
    ed, _ = ctx
    lid = loads.begin(ed, "customer", ["code", "desc", "end"])["load_id"]
    loads.append(ed, lid, 1, "a,x,y\n")
    assert loads.finish(ed, lid)["ok"]


@pytest.mark.parametrize("field,value", [
    ("source_column", "p) FROM stdin;\n\\.\nDROP TABLE x;\nCOPY y (z"), ("parent_table", "pro vince"),
    ("parent_key", "co de"), ("column", "x;y"), ("source_column", 5)])
def test_ref_names_must_be_identifiers(ctx, field, value):    # finding 2
    ed, _ = ctx
    ref = {**REF, field: value}
    cols = ["code", ref["source_column"]] if isinstance(ref["source_column"], str) else ["code"]
    with pytest.raises(loads.LoadError):
        loads.begin(ed, "district", cols, refs=[ref])


def test_cancel_waits_for_finish_and_cannot_undo_it(ctx):      # finding 3
    ed, _ = ctx
    lid = loads.begin(ed, "customer", ["code"])["load_id"]
    loads.append(ed, lid, 1, "a\n")
    loads.finish(ed, lid)
    with pytest.raises(loads.LoadError, match="finished"):
        loads.cancel(ed, lid)
    assert StagedLoad.objects.get(pk=lid).status == "finished"


def test_cancel_rechecks_status_under_lock(ctx, monkeypatch):  # finding 3 (the race itself)
    ed, _ = ctx
    lid = loads.begin(ed, "customer", ["code"])["load_id"]
    real = loads._mine
    def stale(user, load_id):
        load = real(user, load_id)
        StagedLoad.objects.filter(pk=load_id).update(status="finished")   # finish commits after our read
        return load
    monkeypatch.setattr(loads, "_mine", stale)
    with pytest.raises(loads.LoadError, match="finished"):
        loads.cancel(ed, lid)
    assert StagedLoad.objects.get(pk=lid).status == "finished"


def test_total_bytes_limit_never_exceeds_script_cap(ctx):      # finding 4
    ed, appr = ctx
    out = loads.begin(ed, "customer", ["code"])
    from config.siconfig import get_config
    assert out["limits"]["total_bytes"] < get_config()["limits"]["maxScriptBytes"]      # the script cap, minus the header


def test_failed_finish_leaves_no_script(ctx):                  # finding 5
    ed, _ = ctx
    lid = loads.begin(ed, "customer", ["code"])["load_id"]
    loads.append(ed, lid, 1, "a\na\n")
    assert loads.finish(ed, lid)["ok"] is False
    assert Script.objects.count() == 0
    d = os.path.join(storage.loads_dir(), "..")
    assert [f for f in os.listdir(d) if f.endswith(".sql")] == []


def test_expired_loads_do_not_count_against_the_cap(ctx):      # finding 6
    ed, appr = ctx
    tunables.apply(appr, {"loads.maxOpenPerUser": 1})
    lid = loads.begin(ed, "customer", ["code"])["load_id"]
    StagedLoad.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
    assert loads.get(ed, lid)["status"] == "expired"
    loads.begin(ed, "customer", ["code"])


def test_nul_is_refused_per_chunk_and_size_hint_only_for_size(ctx):   # finding 7
    ed, _ = ctx
    lid = loads.begin(ed, "customer", ["code", "desc"])["load_id"]
    out = loads.append(ed, lid, 1, "a,x\nb,y\x00z\n")
    assert out["ok"] is False and out["problems"][0]["row"] == 2 and "NUL" in out["problems"][0]["message"]


def test_ref_parent_key_checked_at_begin(ctx):                 # finding 8
    ed, _ = ctx
    for key in ("label", "nope"):          # not unique / not a column
        with pytest.raises(loads.LoadError, match="parent_key"):
            loads.begin(ed, "district", ["code", "province_code"], refs=[{**REF, "parent_key": key}])
