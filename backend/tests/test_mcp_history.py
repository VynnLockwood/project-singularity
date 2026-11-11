"""MCP tools for change history and the audit log."""
import pytest
from django.test import Client

from accounts.models import Role, User
from approvals.models import Change
from audit.models import AuditLog, record
from mcp_server import tools

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]


def user(role, email=None):
    email = email or f"{role}@example.com"
    return User.objects.create(username=email, email=email, role=role)


def change(requester, **over):
    base = dict(requester=requester, source="mcp", kind="schema", schema="master", status="pending")
    return Change.objects.create(**{**base, **over})


def test_list_changes_newest_first_and_capped():
    ed = user(Role.EDITOR)
    for _ in range(25):
        change(ed)
    out = tools.list_changes(ed)
    ids = [c["id"] for c in out["changes"]]
    assert len(ids) == 20 and out["truncated"] is True
    assert ids == sorted(ids, reverse=True)
    full = tools.list_changes(ed, limit=100)
    assert len(full["changes"]) == 25 and full["truncated"] is False
    assert "sql" not in full["changes"][0]          # summaries only


def test_list_changes_filters():
    ed, other = user(Role.EDITOR), user(Role.EDITOR, "other@example.com")
    a = change(ed, status="applied", kind="data")
    change(ed, status="pending", kind="schema")
    b = change(other, status="applied", kind="schema")
    assert {c["id"] for c in tools.list_changes(ed, status="applied")["changes"]} == {a.pk, b.pk}
    assert [c["id"] for c in tools.list_changes(ed, kind="data")["changes"]] == [a.pk]
    assert [c["id"] for c in tools.list_changes(other, mine=True)["changes"]] == [b.pk]
    assert [c["id"] for c in tools.list_changes(ed, status="applied", kind="data", mine=True, schema="master")["changes"]] == [a.pk]


def test_list_changes_bad_arguments_do_not_raise():
    ed = user(Role.EDITOR)
    for _ in range(3):
        change(ed)
    assert tools.list_changes(ed, status="bogus") == {"changes": [], "truncated": False}
    assert tools.list_changes(ed, kind="bogus")["changes"] == []
    for bad in (0, -3, 10**9, "abc"):
        assert len(tools.list_changes(ed, limit=bad)["changes"]) == 3


def test_list_changes_keeps_history_of_a_database_that_is_gone():
    ed = user(Role.EDITOR)
    old = change(ed, schema="dropped_since", status="applied")   # history outlives a renamed or dropped database
    assert [c["id"] for c in tools.list_changes(ed, schema="dropped_since")["changes"]] == [old.pk]
    assert tools.list_changes(ed, schema="never_existed") == {"changes": [], "truncated": False}
    assert tools.list_changes(ed, schema=7) == {"changes": [], "truncated": False}


def test_audit_log_is_for_approvers():
    assert not tools.allowed(user(Role.EDITOR), "get_audit_log")
    assert tools.allowed(user(Role.APPROVER), "get_audit_log")
    assert tools.allowed(user(Role.VIEWER, "v@example.com"), "list_changes")


def test_audit_log_filters_and_caps():
    ap, ed = user(Role.APPROVER), user(Role.EDITOR)
    for i in range(60):
        record(ap if i % 2 else ed, "change.applied" if i % 3 else "schema.export", target=f"#{i}", details={"i": i})
    out = tools.get_audit_log(ap)
    assert len(out["entries"]) == 50 and out["truncated"] is True
    assert set(out["entries"][0]) == {"id", "at", "actor", "action", "target", "details"}
    ids = [e["id"] for e in out["entries"]]
    assert ids == sorted(ids, reverse=True)
    exports = tools.get_audit_log(ap, action="schema.export", limit=200)
    assert len(exports["entries"]) == 20 and exports["truncated"] is False
    assert {e["action"] for e in exports["entries"]} == {"schema.export"}
    mine = tools.get_audit_log(ap, actor="APPROVER@example.com", limit=200)
    assert len(mine["entries"]) == 30 and {e["actor"] for e in mine["entries"]} == {"approver@example.com"}
    assert tools.get_audit_log(ap, action="nothing.like.this") == {"entries": [], "truncated": False}


def test_rest_changes_and_audit_still_work():
    ed = user(Role.EDITOR)
    a = change(ed, status="applied")
    change(user(Role.EDITOR, "other@example.com"), status="applied")
    change(ed, status="pending")
    record(ed, "x.y", target="t")
    record(ed, "other", target="t")
    c = Client()
    c.force_login(ed)
    got = c.get("/api/changes?status=applied&mine=1").json()
    assert [x["id"] for x in got["changes"]] == [a.pk]
    assert len(c.get("/api/changes").json()["changes"]) == 3
    assert c.get("/api/changes?schema=nope").json() == {"changes": [], "more": False}
    entries = c.get("/api/audit?action=x.y").json()["entries"]
    assert [e["action"] for e in entries] == ["x.y"] and set(entries[0]) == {"id", "at", "actor", "action", "target", "details"}
    assert AuditLog.objects.count() >= 2
