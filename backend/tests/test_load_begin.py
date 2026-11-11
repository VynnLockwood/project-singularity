import os

import pytest

from accounts.models import Role, User
from audit.models import AuditLog
from scripts import loads, storage
from scripts.models import StagedLoad
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
def users():
    u = make_users()
    applied(u[Role.APPROVER], [
        {"op": "create_table", "name": "province",
         "columns": [{"name": "code", "type": "text", "unique": True}]},
        {"op": "create_table", "name": "customer",
         "columns": [{"name": "name_en", "type": "text", "nullable": False}, {"name": "age", "type": "integer"}]}])
    return u


def test_begin_returns_limits(users):
    out = loads.begin(users[Role.EDITOR], "customer", ["name_en", "age"])
    assert out["next_seq"] == 1 and out["columns"] == ["name_en", "age"]
    assert out["limits"]["chunk_rows"] == 5000 and out["limits"]["total_rows"] == 200000
    assert os.path.getsize(os.path.join(storage.loads_dir(), StagedLoad.objects.get().path)) == 0


@pytest.mark.parametrize("table,cols,text", [
    ("nope", ["a"], "does not exist"), ("customer", ["name_en", "zzz"], "no column zzz"),
    ("customer", ["name_en", "name_en"], "twice"), ("customer", ["age"], "name_en")])
def test_bad_begin(users, table, cols, text):
    with pytest.raises(loads.LoadError, match=text):
        loads.begin(users[Role.EDITOR], table, cols)


REF = {"column": "age", "parent_table": "province", "parent_key": "code", "source_column": "province_code"}


def test_ref_source_column_may_be_extra(users):
    out = loads.begin(users[Role.EDITOR], "customer", ["name_en", "province_code"], refs=[REF])
    assert out["columns"] == ["name_en", "province_code"]


def test_ref_column_not_in_columns(users):
    with pytest.raises(loads.LoadError, match="must not also be in columns"):
        loads.begin(users[Role.EDITOR], "customer", ["name_en", "age", "province_code"], refs=[REF])


def test_open_load_cap(users):
    tunables.apply(users[Role.APPROVER], {"loads.maxOpenPerUser": 1})
    loads.begin(users[Role.EDITOR], "customer", ["name_en"])
    with pytest.raises(loads.LoadError, match="open loads"):
        loads.begin(users[Role.EDITOR], "customer", ["name_en"])


def test_viewer_refused(users):
    with pytest.raises(PermissionError):
        loads.begin(users[Role.VIEWER], "customer", ["name_en"])


def test_other_users_load_is_missing_and_approver_can_read(users):
    lid = loads.begin(users[Role.EDITOR], "customer", ["name_en"])["load_id"]
    other = User.objects.create(username="e2@x.io", email="e2@x.io", role=Role.EDITOR)
    for fn in (loads.get, loads.cancel):
        with pytest.raises(loads.LoadError, match="does not exist"):
            fn(other, lid)
    assert loads.get(users[Role.APPROVER], lid)["status"] == "open"


def test_cancel_removes_file_and_is_final(users):
    lid = loads.begin(users[Role.EDITOR], "customer", ["name_en"])["load_id"]
    path = os.path.join(storage.loads_dir(), StagedLoad.objects.get().path)
    assert loads.cancel(users[Role.EDITOR], lid)["status"] == "cancelled"
    assert not os.path.exists(path)
    assert loads.cancel(users[Role.EDITOR], lid)["status"] == "cancelled"   # repeat is harmless
    StagedLoad.objects.update(status="finished")
    with pytest.raises(loads.LoadError, match="finished"):
        loads.cancel(users[Role.EDITOR], lid)


def test_unknown_load_id(users):
    with pytest.raises(loads.LoadError, match="does not exist"):
        loads.get(users[Role.EDITOR], 999)


def test_audit_events(users):
    lid = loads.begin(users[Role.EDITOR], "customer", ["name_en"])["load_id"]
    loads.cancel(users[Role.EDITOR], lid)
    acts = list(AuditLog.objects.filter(action__startswith="load.").values_list("action", flat=True))
    assert sorted(acts) == ["load.begin", "load.cancel"]
