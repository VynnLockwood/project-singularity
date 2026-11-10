"""propose_sql over MCP and chat (plan C task 6)."""
import asyncio

import pytest

from accounts.models import Role, User
from approvals.models import Change
from chat import toolspecs
from mcp_server import app as mcp_app
from mcp_server import tools
from settings_app import tunables

from .ops_helpers import make_table

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]
REASON = "fix the typo in column a"


@pytest.fixture(autouse=True)
def fresh_settings():
    from config.siconfig import invalidate_overrides
    invalidate_overrides()
    yield
    invalidate_overrides()


def user(role, name=None):
    n = name or role
    return User.objects.create(username=f"{n}@x.com", email=f"{n}@x.com", role=role)


def test_editor_and_approver_get_a_pending_change_that_waits_for_the_web_app():
    make_table()
    for role in (Role.EDITOR, Role.APPROVER):
        got = tools.IMPL["propose_sql"](user(role), sql="UPDATE master.t SET a = 'x'", reason=REASON)
        assert got["kind"] == "sql" and got["status"] == "pending"
        assert "web app" in got["next"] and "cannot approve" in got["next"]


def test_roles_and_the_switch():
    assert tools.TOOL_ROLES["propose_sql"] == "editor"
    assert not tools.allowed(user(Role.VIEWER), "propose_sql")
    assert tools.allowed(user(Role.EDITOR, "e"), "propose_sql")
    tunables.apply(user(Role.APPROVER, "ap"), {"sql.enabled": False})
    assert not tools.allowed(user(Role.APPROVER, "ap2"), "propose_sql")


def test_approve_change_refuses_raw_sql_even_for_an_approver():
    make_table()
    ap = user(Role.APPROVER)
    got = tools.IMPL["propose_sql"](ap, sql="UPDATE master.t SET a = 'x'", reason=REASON)
    with pytest.raises(tools.ToolFailure, match="approved by a person in the web app"):
        tools.IMPL["approve_change"](ap, change_id=got["id"])
    assert Change.objects.get(pk=got["id"]).status == "pending"


def test_reject_works_over_mcp():
    make_table()
    ap = user(Role.APPROVER)
    got = tools.IMPL["propose_sql"](ap, sql="UPDATE master.t SET a = 'x'", reason=REASON)
    assert tools.IMPL["reject_change"](ap, change_id=got["id"], note="no")["status"] == "rejected"


def test_a_refused_statement_is_a_tool_failure_with_the_statement_number():
    make_table()
    with pytest.raises(tools.ToolFailure, match="statement 1"):
        tools.IMPL["propose_sql"](user(Role.EDITOR), sql="SET ROLE postgres", reason=REASON)


def test_registered_with_the_server_and_chat():
    registered = {t.name for t in asyncio.run(super(type(mcp_app.mcp), mcp_app.mcp).list_tools())}
    assert "propose_sql" in registered and "propose_sql" in toolspecs.SPECS


def test_descriptions_and_instructions_guide_the_ai():
    d = tools.DESCRIPTIONS["propose_sql"]
    assert "editor" in d and "approver" in d and "web app" in d
    assert toolspecs.SPECS["propose_sql"][0] == d
    assert "propose_sql" in mcp_app.INSTRUCTIONS and "only when no operation fits" in mcp_app.INSTRUCTIONS


def test_the_chat_can_propose_sql_as_a_web_change():
    import json
    from chat import orchestrator
    from chat.models import Conversation
    make_table()
    ed = user(Role.EDITOR)
    conv = Conversation.objects.create(user=ed)
    out = orchestrator.run_tool(ed, conv, "propose_sql", json.dumps({"sql": "UPDATE master.t SET a = 'x'", "reason": REASON}))
    assert out["kind"] == "sql" and out["source"] == "web"
    assert Change.objects.get(pk=out["id"]).conversation_id == conv.pk
    assert "propose_sql" in orchestrator.SYSTEM_PROMPT
