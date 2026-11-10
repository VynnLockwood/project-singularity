"""MCP tools that act on an existing change: revert and re-check."""
import pytest

from accounts.models import Role
from mcp_server import tools

from .mcp_helpers import applied, create_table, make_users

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]


@pytest.fixture
def users():
    return make_users()


def table_names(user):
    return [t["name"] for t in tools.list_tables(user)["tables"]]


def test_revert_undoes_an_applied_change(users):
    ap = users[Role.APPROVER]
    ch = applied(ap, [create_table("region")])
    assert table_names(ap) == ["region"]
    rev = tools.revert_change(ap, ch["id"])
    assert rev["status"] == "applied" and rev["source"] == "revert"
    assert table_names(ap) == []
    assert rev["next"].startswith("Done")


def test_revert_refuses_what_cannot_be_undone(users):
    ap = users[Role.APPROVER]
    pending = tools.propose_change(ap, [create_table("region")])
    with pytest.raises(tools.ToolFailure, match="only applied changes can be reverted"):
        tools.revert_change(ap, pending["id"])
    tools.approve_change(ap, pending["id"])
    dropped = applied(ap, [{"op": "drop_table", "table": "region", "reason": "created by mistake"}])
    with pytest.raises(tools.ToolFailure, match="cannot be undone automatically"):
        tools.revert_change(ap, dropped["id"])
    made = applied(ap, [create_table("zone")])
    tools.revert_change(ap, made["id"])
    with pytest.raises(tools.ToolFailure, match="already reverted"):
        tools.revert_change(ap, made["id"])
    with pytest.raises(tools.ToolFailure, match="does not exist"):
        tools.revert_change(ap, 999999)


def test_revert_is_for_approvers(users):
    assert not tools.allowed(users[Role.EDITOR], "revert_change")
    assert tools.allowed(users[Role.APPROVER], "revert_change")
    assert tools.allowed(users[Role.EDITOR], "recheck_change")
    assert not tools.allowed(users[Role.VIEWER], "recheck_change")


def test_recheck_a_stale_change(users):
    ed, ap = users[Role.EDITOR], users[Role.APPROVER]
    applied(ap, [create_table("province")])
    add = lambda name: [{"op": "add_column", "table": "province", "column": {"name": name, "type": "text", "nullable": True}}]
    waiting = tools.propose_change(ed, add("name_th"))
    assert waiting["status"] == "pending"
    applied(ap, add("name_en_short"))                     # the same table moves under the waiting change
    stale = tools.approve_change(ap, waiting["id"])
    assert stale["status"] == "stale" and f"recheck_change with change_id={waiting['id']}" in stale["next"]
    again = tools.recheck_change(ed, waiting["id"])
    assert again["status"] == "pending" and again["id"] == waiting["id"]
    assert tools.approve_change(ap, waiting["id"])["status"] == "applied"


def test_recheck_refuses_bad_input(users):
    ed, ap = users[Role.EDITOR], users[Role.APPROVER]
    with pytest.raises(tools.ToolFailure, match="does not exist"):
        tools.recheck_change(ed, 999999)
    done = applied(ap, [create_table("region")])
    with pytest.raises(tools.ToolFailure):
        tools.recheck_change(ed, done["id"])             # only a stale change can be re-checked
