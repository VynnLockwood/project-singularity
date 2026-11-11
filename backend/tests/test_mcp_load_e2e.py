import time

import pytest

from accounts.models import Role
from mcp_server import tools
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
            {"name": "name_en", "type": "text"}, {"name": "age", "type": "integer"}]},
        {"op": "create_table", "name": "province", "columns": [{"name": "code", "type": "text", "unique": True}]},
        {"op": "create_table", "name": "district", "columns": [{"name": "code", "type": "text", "unique": True},
                                                                {"name": "province_id", "type": "integer"}]}])
    return u[Role.EDITOR], u[Role.APPROVER]


def count(user, table):
    return tools.run_query(user, f"SELECT count(*) AS n FROM {table}")["rows"][0][0]


def load(ed, table, columns, chunks, **kw):
    lid = tools.begin_load(ed, table, columns, **kw)["load_id"]
    for i, c in enumerate(chunks, 1):
        out = tools.append_rows(ed, lid, i, c)
        assert out["ok"], out
    return lid


def test_load_approve_rows_present_then_revert_deactivates_them(ctx):
    ed, appr = ctx
    chunks = ["".join(f"C-{i},Name {i},{i}\n" for i in range(k * 1000, k * 1000 + 1000)) for k in range(3)]
    chunks[0] += '"T-\tab","line1\nline2",\n"\\N","",5\n'
    lid = load(ed, "customer", ["code", "name_en", "age"], chunks)
    out = tools.finish_load(ed, lid)
    assert out["ok"], out
    done = tools.approve_change(appr, out["change_id"])
    assert done["status"] == "applied", done
    assert count(appr, "customer") == 3002
    rows = {r[0]: r for r in tools.run_query(
        appr, "SELECT code, name_en, age FROM customer WHERE code IN ('T-\tab', '\\N', 'C-7')")["rows"]}
    assert rows["T-\tab"][1:] == ["line1\nline2", None]
    assert rows["\\N"][1:] == ["", 5]      # NULL vs empty string stay distinct
    assert rows["C-7"][1] == "Name 7"
    rev = tools.revert_change(appr, out["change_id"])
    assert rev["status"] == "applied", rev
    # undo is the existing soft delete: the loaded rows are deactivated, not erased
    assert tools.run_query(appr, "SELECT count(*) FROM customer WHERE is_active")["rows"][0][0] == 0


def test_unique_violation_found_at_finish_names_the_key_and_load_stays_open(ctx):
    ed, _ = ctx
    lid = load(ed, "customer", ["code", "name_en", "age"], ["a,x,1\nb,y,2\n", "a,z,3\n"])
    out = tools.finish_load(ed, lid)
    assert out["ok"] is False and out["report"]["dry_run"]["ok"] is False, out
    assert "customer_code_key" in out["report"]["dry_run"]["error"] and "(a)" in out["report"]["dry_run"]["error"]
    assert tools.get_load(ed, lid)["status"] == "open"


def test_external_ref_load_end_to_end(ctx):
    ed, appr = ctx
    tools.approve_change(appr, tools.propose_change(appr, [
        {"op": "insert_rows", "table": "province", "rows": [{"code": "10"}, {"code": "20"}]}])["id"])
    ref = {"column": "province_id", "parent_table": "province", "parent_key": "code", "source_column": "province_code"}
    lid = load(ed, "district", ["code", "province_code"], ["d1,10\nd2,20\nd3,10\n"], refs=[ref])
    out = tools.finish_load(ed, lid)
    assert out["ok"], out
    assert tools.approve_change(appr, out["change_id"])["status"] == "applied"
    rows = tools.run_query(appr, "SELECT d.code, p.code AS pc FROM district d JOIN province p "
                                 "ON p.province_id = d.province_id ORDER BY d.code")["rows"]
    assert [tuple(r) for r in rows] == [("d1", "10"), ("d2", "20"), ("d3", "10")]


@pytest.mark.slow
def test_100k_rows(ctx):
    ed, appr = ctx
    started = time.monotonic()
    lid = tools.begin_load(ed, "customer", ["code", "name_en", "age"])["load_id"]
    n = 0
    for seq in range(1, 21):
        chunk = "".join(f"K-{i},Customer number {i},{i % 90}\n" for i in range(n, n + 5000))
        out = tools.append_rows(ed, lid, seq, chunk)
        assert out["ok"], out
        n += 5000
    fin = tools.finish_load(ed, lid)
    assert fin["ok"], fin
    assert tools.approve_change(appr, fin["change_id"])["status"] == "applied"
    assert count(appr, "customer") == 100000
    assert time.monotonic() - started < 120
