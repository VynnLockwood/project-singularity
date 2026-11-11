import pytest

from accounts.models import Role
from mcp_server import tools
from mcp_server.tools import ToolFailure
from tests.mcp_helpers import applied, make_users

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]

LOAD_TOOLS = ("begin_load", "append_rows", "finish_load", "get_load", "cancel_load")


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
    applied(u[Role.APPROVER], [{"op": "create_table", "name": "customer", "columns": [
        {"name": "code", "type": "text", "unique": True}, {"name": "name_en", "type": "text"}]}])
    return u


def test_tools_exist_for_editor_and_not_viewer(users):
    for t in LOAD_TOOLS:
        assert tools.allowed(users[Role.EDITOR], t) and tools.allowed(users[Role.APPROVER], t)
        assert not tools.allowed(users[Role.VIEWER], t)
        assert tools.TOOL_ROLES[t] == "editor"


def test_full_flow_returns_change(users):
    ed = users[Role.EDITOR]
    lid = tools.begin_load(ed, "customer", ["code", "name_en"])["load_id"]
    assert tools.append_rows(ed, lid, 1, "C-1,Acme\nC-2,\"Smith, Jones\"\n")["ok"]
    assert tools.get_load(ed, lid)["total_rows"] == 2
    out = tools.finish_load(ed, lid, "first")
    assert out["ok"] and out["change_id"] and out["next"]
    assert tools.get_load(ed, lid)["status"] == "finished"


def test_bad_chunk_returns_problems_not_exception(users):
    ed = users[Role.EDITOR]
    lid = tools.begin_load(ed, "customer", ["code", "name_en"])["load_id"]
    out = tools.append_rows(ed, lid, 1, "only-one-field\n")
    assert out["ok"] is False and out["problems"][0]["row"] == 1


def test_misuse_raises_tool_failure(users):
    ed = users[Role.EDITOR]
    lid = tools.begin_load(ed, "customer", ["code", "name_en"])["load_id"]
    with pytest.raises(ToolFailure, match="expected seq 1"):
        tools.append_rows(ed, lid, 3, "a,b\n")
    with pytest.raises(ToolFailure, match="does not exist"):
        tools.get_load(ed, 4242)
    with pytest.raises(ToolFailure, match="does not exist"):
        tools.begin_load(ed, "nope", ["a"])
    with pytest.raises(ToolFailure):
        tools.begin_load(ed, "customer", ["code"], schema="nodb")
    with pytest.raises(ToolFailure):
        tools.begin_load(users[Role.VIEWER], "customer", ["code"])


def test_get_rules_mentions_bulk_loading(users):
    r = tools.get_rules(users[Role.EDITOR])
    assert "begin_load" in str(r["bulk_loading"]) and "finish_load" in str(r["bulk_loading"])


def test_insert_rows_help_points_at_begin_load():
    assert "begin_load" in tools.OPERATIONS_HELP
