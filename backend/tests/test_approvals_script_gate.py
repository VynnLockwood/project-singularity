"""Script-sourced changes need a real approver, and MCP agents cannot decide at all.

No database: `can_decide` is pure policy over a user and a change.
"""
from types import SimpleNamespace

from approvals.service import can_decide
from mcp_server.tools import TOOL_ROLES

EDITOR = SimpleNamespace(pk=1, can_edit=True, can_approve=False)
APPROVER = SimpleNamespace(pk=2, can_edit=True, can_approve=True)
PLAIN_DATA = SimpleNamespace(kind="data", source="web", requester_id=1)
SCRIPT_DATA = SimpleNamespace(kind="data", source="script", requester_id=1)


def test_editor_cannot_decide_own_script_change():
    assert not can_decide(EDITOR, SCRIPT_DATA)


def test_approver_can_decide_a_script_change():
    assert can_decide(APPROVER, SCRIPT_DATA)


def test_editor_still_decides_own_plain_data_change():
    assert can_decide(EDITOR, PLAIN_DATA)


def test_mcp_decision_tools_are_approver_only():
    assert TOOL_ROLES["approve_change"] == "approver"
    assert TOOL_ROLES["reject_change"] == "approver"
    assert TOOL_ROLES["submit_script"] == "editor"
