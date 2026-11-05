"""MCP tool logic as plain functions: (user, ...) -> JSON-able dict.

Kept apart from the MCP SDK on purpose: every rule (roles, the gate, approvals)
lives in the shared services, and this file only shapes inputs and outputs for
an AI client. The SDK adapter is in `app.py`.
"""
from __future__ import annotations

from approvals import service
from approvals.models import Change
from config.siconfig import get_config
from schema import introspect, query
from settings_app import services as rule_services

# Minimum role for each tool. Tools above a user's role are hidden from their client.
TOOL_ROLES = {
    "get_rules": "viewer",
    "list_tables": "viewer",
    "describe_table": "viewer",
    "query": "viewer",
    "propose_change": "editor",
    "get_change": "editor",
    "approve_change": "editor",
    "reject_change": "editor",
    "list_pending": "approver",
}

RANK = {"viewer": 0, "editor": 1, "approver": 2}


class ToolFailure(Exception):
    """An error the AI should see and can act on."""


def allowed(user, tool: str) -> bool:
    need = TOOL_ROLES.get(tool)
    return need is not None and user is not None and user.rank >= RANK[need]


def get_rules(user) -> dict:
    return {
        "instructions": (
            "Follow these rules when you design tables and columns. Rules with severity 'block' reject a "
            "change; 'warn' rules are shown to the reviewer. The gate checks every proposal again."
        ),
        "rules": [
            {k: r[k] for k in ("name", "type", "severity", "description", "params")}
            for r in rule_services.active_rules() if r["enabled"]
        ],
    }


def list_tables(user) -> dict:
    return {"tables": introspect.list_tables()}


def describe_table(user, table: str) -> dict:
    d = introspect.describe_table(table)
    if d is None:
        raise ToolFailure(f"table {table!r} does not exist. Call list_tables to see what exists.")
    return d


def run_query(user, sql: str, limit: int | None = None) -> dict:
    limits = get_config()["limits"]
    cap = limits["maxQueryRows"]
    try:
        return query.run_select(sql, limit=min(limit or cap, cap), timeout_ms=limits["queryTimeoutMs"])
    except query.QueryError as exc:
        raise ToolFailure(str(exc)) from exc


def _next_step(user, change: Change) -> str:
    if change.status == "rejected" and change.decided_by_id is None:
        return "The gate blocked this change. Fix the problems in 'check' (use the suggestions) and call propose_change again."
    if change.status == "pending":
        if service.can_decide(user, change):
            return (
                "Show the user the 'diff' and any warnings, and ask them to confirm. Only after they say yes, "
                f"call approve_change with change_id={change.pk}."
            )
        return (
            f"This change needs an approver. Tell the user it is waiting as change #{change.pk}; "
            "an approver can approve it in the web app."
        )
    if change.status == "applied":
        return "Done. The change was applied."
    if change.status == "stale":
        return "The schema or rules changed since this was checked. Call propose_change again with the same operations."
    if change.status == "failed":
        return "The database refused the change and nothing was changed. See 'error'."
    return ""


def _result(user, change: Change) -> dict:
    return {**service.serialize(change), "next": _next_step(user, change)}


def propose_change(user, operations: list, prompt: str = "") -> dict:
    try:
        change = service.propose(user, operations, source="mcp", prompt=prompt[:10_000])
    except service.ChangeError as exc:
        raise ToolFailure(exc.message) from exc
    return _result(user, change)


def get_change(user, change_id: int) -> dict:
    change = Change.objects.select_related("requester", "decided_by").filter(pk=change_id).first()
    if change is None:
        raise ToolFailure(f"change #{change_id} does not exist")
    return _result(user, change)


def approve_change(user, change_id: int) -> dict:
    try:
        change = service.approve(user, change_id)
    except service.ChangeError as exc:
        raise ToolFailure(exc.message) from exc
    return _result(user, change)


def reject_change(user, change_id: int, note: str = "") -> dict:
    try:
        change = service.reject(user, change_id, note[:2000])
    except service.ChangeError as exc:
        raise ToolFailure(exc.message) from exc
    return _result(user, change)


def list_pending(user) -> dict:
    items = service.pending_for(user).select_related("requester", "decided_by")[:50]
    return {"pending": [service.serialize(c, detail=False) for c in items]}


IMPL = {
    "get_rules": get_rules,
    "list_tables": list_tables,
    "describe_table": describe_table,
    "query": run_query,
    "propose_change": propose_change,
    "get_change": get_change,
    "approve_change": approve_change,
    "reject_change": reject_change,
    "list_pending": list_pending,
}
