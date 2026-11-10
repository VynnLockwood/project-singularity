"""MCP tools for duplicates: find, list, look at, merge, dismiss."""
import json

import pytest
from django.test import Client

from accounts.models import Role
from audit.models import AuditLog
from mcp_server import tools

from .mcp_helpers import applied, make_users

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]

NAMES = ["Acme Trading Co., Ltd.", "ACME TRADING COMPANY LIMITED", "Zenith Foods", "Orion Steel"]


@pytest.fixture
def users():
    u = make_users()
    ap = u[Role.APPROVER]
    applied(ap, [
        {"op": "create_table", "name": "supplier", "columns": [{"name": "name", "type": "text"}]},
        {"op": "create_table", "name": "purchase", "columns": [
            {"name": "supplier_id", "type": "bigint", "references": {"table": "supplier"}}]},
    ])
    applied(ap, [{"op": "insert_rows", "table": "supplier", "rows": [{"name": n} for n in NAMES]}])
    applied(ap, [{"op": "insert_rows", "table": "purchase", "rows": [{"supplier_id": 2}, {"supplier_id": 3}]}])
    return u


def found(users):
    out = tools.find_duplicates(users[Role.EDITOR], "supplier")
    assert out["groups"] >= 1, out
    return out["groups_found"][0]


def test_find_lists_the_group(users):
    out = tools.find_duplicates(users[Role.EDITOR], "supplier")
    assert out["table"] == "supplier" and out["truncated"] is False
    assert sorted(out["groups_found"][0]["memberIds"]) == [1, 2]
    listed = tools.list_duplicates(users[Role.VIEWER], table="supplier")
    assert [g["id"] for g in listed["groups"]] == [out["groups_found"][0]["id"]] and listed["truncated"] is False
    assert AuditLog.objects.filter(action="duplicates.scanned").count() == 1


def test_get_shows_records_side_by_side(users):
    g = found(users)
    d = tools.get_duplicate(users[Role.VIEWER], g["id"])
    assert d["primaryKey"] == "supplier_id" and [m["supplier_id"] for m in d["members"]] == [1, 2]
    assert d["referencedBy"] == [{"table": "purchase", "column": "supplier_id"}]


def test_merge_proposes_a_data_change(users):
    ed, ap = users[Role.EDITOR], users[Role.APPROVER]
    g = found(users)
    out = tools.merge_duplicates(ed, g["id"], 1)
    ch = out["change"]
    assert ch["kind"] == "data" and ch["status"] == "pending" and ch["source"] == "mcp" and ch["next"]
    assert out["group"]["status"] == "merge_pending"
    assert tools.approve_change(ap, ch["id"])["status"] == "applied"
    assert tools.list_duplicates(ed, table="supplier", status="merged")["groups"][0]["id"] == g["id"]
    assert tools.read_rows(ed, "supplier")["total"] == 3            # the other record is deactivated
    assert {r["supplier_id"] for r in tools.read_rows(ed, "purchase")["rows"]} == {1, 3}


def test_merge_refuses_bad_input(users):
    ed = users[Role.EDITOR]
    g = found(users)
    with pytest.raises(tools.ToolFailure, match="one of the group's records"):
        tools.merge_duplicates(ed, g["id"], 4)
    with pytest.raises(tools.ToolFailure, match="survivor_id"):
        tools.merge_duplicates(ed, g["id"], True)
    with pytest.raises(tools.ToolFailure, match="does not exist.*list_duplicates"):
        tools.merge_duplicates(ed, 999999, 1)
    tools.merge_duplicates(ed, g["id"], 1)
    with pytest.raises(tools.ToolFailure, match="already has a merge in progress"):
        tools.merge_duplicates(ed, g["id"], 1)


def test_merge_refuses_a_dismissed_group(users):
    ed = users[Role.EDITOR]
    g = found(users)
    tools.dismiss_duplicate(ed, g["id"])
    with pytest.raises(tools.ToolFailure, match="dismissed.*find_duplicates"):
        tools.merge_duplicates(ed, g["id"], 1)
    assert tools.list_changes(ed, kind="data", status="pending")["changes"] == []     # nothing was proposed


def test_merge_of_a_group_whose_table_was_dropped_says_what_to_do(users):
    ap = users[Role.APPROVER]
    g = found(users)
    applied(ap, [{"op": "drop_table", "table": "purchase", "reason": "test"}])
    applied(ap, [{"op": "drop_table", "table": "supplier", "reason": "test"}])
    with pytest.raises(tools.ToolFailure, match="dismiss_duplicate"):
        tools.merge_duplicates(ap, g["id"], 1)


def test_dismiss_and_list_by_status(users):
    ed = users[Role.EDITOR]
    g = found(users)
    assert tools.dismiss_duplicate(ed, g["id"])["status"] == "dismissed"
    assert tools.list_duplicates(ed, table="supplier")["groups"] == []          # default: open only
    assert [x["id"] for x in tools.list_duplicates(ed, table="supplier", status="dismissed")["groups"]] == [g["id"]]
    assert [x["id"] for x in tools.list_duplicates(ed, status=None)["groups"]] == [g["id"]]
    assert AuditLog.objects.filter(action="duplicates.dismissed").count() == 1
    with pytest.raises(tools.ToolFailure, match="does not exist"):
        tools.dismiss_duplicate(ed, 999999)


def test_group_whose_table_was_dropped(users):
    ap = users[Role.APPROVER]
    g = found(users)
    applied(ap, [{"op": "drop_table", "table": "purchase", "reason": "test"}])
    applied(ap, [{"op": "drop_table", "table": "supplier", "reason": "test"}])
    with pytest.raises(tools.ToolFailure, match="dismiss_duplicate"):
        tools.get_duplicate(ap, g["id"])


def test_unknown_table_or_schema(users):
    ed = users[Role.EDITOR]
    with pytest.raises(tools.ToolFailure):
        tools.find_duplicates(ed, "nope")
    with pytest.raises(tools.ToolFailure, match="list_schemas"):
        tools.find_duplicates(ed, "supplier", schema="nope")
    with pytest.raises(tools.ToolFailure, match="list_schemas"):
        tools.list_duplicates(ed, schema="nope")
    with pytest.raises(tools.ToolFailure, match="fields"):
        tools.find_duplicates(ed, "supplier", fields="name")


def test_roles(users):
    v, ed = users[Role.VIEWER], users[Role.EDITOR]
    assert tools.allowed(v, "list_duplicates") and tools.allowed(v, "get_duplicate")
    assert not any(tools.allowed(v, t) for t in ("find_duplicates", "merge_duplicates", "dismiss_duplicate"))
    assert all(tools.allowed(ed, t) for t in ("find_duplicates", "merge_duplicates", "dismiss_duplicate"))


def test_rest_duplicates_unchanged(users):
    g = found(users)
    c = Client()
    c.force_login(users[Role.EDITOR])
    listed = c.get("/api/duplicates?table=supplier").json()
    assert [x["id"] for x in listed["groups"]] == [g["id"]] and listed["tables"] == {"supplier": 1}
    assert c.get("/api/duplicates?schema=nope").status_code == 400
    done = c.post(f"/api/duplicates/{g['id']}/dismiss", json.dumps({}), content_type="application/json")
    assert done.status_code == 200 and done.json()["status"] == "dismissed"
    assert c.post("/api/duplicates/999999/dismiss", json.dumps({}), content_type="application/json").status_code == 404
    assert c.get("/api/duplicates?table=supplier&status=dismissed").json()["groups"][0]["id"] == g["id"]
