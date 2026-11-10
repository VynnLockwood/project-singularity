"""The new tools as the web chat assistant gets them: nothing is applied without a person, and nothing dead-ends."""
import json

import pytest

from accounts.models import Role
from audit.models import AuditLog
from chat import orchestrator, toolspecs
from chat.models import Conversation
from mcp_server import tools

from .mcp_helpers import applied, create_table, make_users

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]


@pytest.fixture
def users():
    return make_users()


def chat(user, name, **args):
    conv = Conversation.objects.create(user=user, model="test/model")
    return conv, orchestrator.run_tool(user, conv, name, json.dumps(args))


def test_the_assistant_cannot_revert_because_a_revert_applies_itself(users):
    ap = users[Role.APPROVER]
    made = applied(ap, [create_table("region")])
    assert "revert_change" not in toolspecs.SPECS
    _, out = chat(ap, "revert_change", change_id=made["id"])
    assert "not available" in out["error"]
    assert [t["name"] for t in tools.list_tables(ap)["tables"]] == ["region"]      # nothing was undone


def test_a_merge_proposed_in_chat_is_linked_to_the_conversation_and_decided_by_the_person(users):
    ed, ap = users[Role.EDITOR], users[Role.APPROVER]
    applied(ap, [{"op": "create_table", "name": "supplier", "columns": [{"name": "name", "type": "text"}]}])
    applied(ap, [{"op": "insert_rows", "table": "supplier",
                  "rows": [{"name": "Acme Trading Co., Ltd."}, {"name": "ACME TRADING COMPANY LIMITED"}]}])
    group = tools.find_duplicates(ed, "supplier")["groups_found"][0]
    conv, out = chat(ed, "merge_duplicates", group_id=group["id"], survivor_id=1)
    ch = out["change"]
    assert ch["status"] == "pending" and ch["source"] == "web"
    assert "approve_change" not in ch["next"] and "review" in ch["next"].lower()
    from approvals.models import Change
    assert Change.objects.get(pk=ch["id"]).conversation_id == conv.pk


def test_an_export_asked_for_in_chat_fits_the_chat_and_says_it_was_cut(users):
    ed, ap = users[Role.EDITOR], users[Role.APPROVER]
    applied(ap, [create_table("region")])
    # about 35 KB of data: under the MCP inline limit (200 KB), over what one chat tool result may hold (20 KB)
    applied(ap, [{"op": "insert_rows", "table": "region",
                  "rows": [{"name_en": f"Region number {i} with a long descriptive name"} for i in range(300)]}])
    assert tools.export_schema(ed, include_data=True)["truncated"] is False
    before = AuditLog.objects.filter(action="schema.export").count()
    _, out = chat(ed, "export_schema", include_data=True)
    assert "error" not in out, out
    assert out["truncated"] is True and "web app" in out["note"]
    assert len(json.dumps(out, ensure_ascii=False)) <= orchestrator.MAX_TOOL_OUTPUT
    assert AuditLog.objects.filter(action="schema.export").count() == before + 1


def test_rows_read_in_chat_come_in_small_pages(users):
    ed, ap = users[Role.EDITOR], users[Role.APPROVER]
    applied(ap, [create_table("region")])
    applied(ap, [{"op": "insert_rows", "table": "region", "rows": [{"name_en": f"R{i}"} for i in range(60)]}])
    _, out = chat(ed, "read_rows", table="region", limit=200)
    assert out["limit"] == orchestrator.CHAT_PAGE and len(out["rows"]) == orchestrator.CHAT_PAGE
    assert out["truncated"] is True and out["total"] == 60
    _, small = chat(ed, "read_rows", table="region", limit=5)
    assert len(small["rows"]) == 5
