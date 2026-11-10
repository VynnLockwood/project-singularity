"""MCP tool logic as plain functions: (user, ...) -> JSON-able dict.

Kept apart from the MCP SDK on purpose: every rule (roles, the gate, approvals)
lives in the shared services, and this file only shapes inputs and outputs for
an AI client. The SDK adapter is in `app.py`.
"""
from __future__ import annotations

from approvals import service
from approvals.models import Change
from scripts import service as script_service, storage
from config.siconfig import get_config
from schema import introspect, query
from settings_app import services as rule_services
from settings_app import tunables

# Minimum role for each tool. Tools above a user's role are hidden from their client.
TOOL_ROLES = {
    "get_rules": "viewer",
    "list_schemas": "viewer",
    "list_tables": "viewer",
    "describe_table": "viewer",
    "query": "viewer",
    "search_schema": "viewer",
    "propose_change": "editor",
    "get_settings": "viewer",
    "propose_config_change": "editor",
    "list_users": "approver",
    "get_change": "editor",
    "approve_change": "approver",   # deciding (not proposing) a change is a human's job
    "reject_change": "approver",
    "list_pending": "approver",
    "submit_script": "editor",
    "validate_script": "editor",
    "get_script": "editor",
    "list_changes": "viewer",
    "read_rows": "viewer",
    "revert_change": "approver",
    "recheck_change": "editor",
    "get_audit_log": "approver",   # audit details can hold other people's prompts and emails
}

RANK = {"viewer": 0, "editor": 1, "approver": 2}

# What each newer tool is for, in the words an AI client reads. One text, used by both the MCP
# server (mcp_server/app.py) and the web chat (chat/toolspecs.py), so the two cannot drift apart.
DESCRIPTIONS = {
    "list_changes": (
        "History of proposed changes, newest first: pending, applied, rejected, failed or stale. Filter by status, "
        "kind (schema, data, config), schema, or mine=true for your own. Summaries only; call get_change for the diff."
    ),
    "get_audit_log": (
        "Who did what and when (approvers only): sign-ins, proposals, decisions, exports, settings changes. "
        "Filter by action or by actor email."
    ),
    "read_rows": (
        "Read rows of one table, a page at a time, without writing SQL: offset, limit (max 200), sort, direction "
        "(asc or desc), filters {column: text contained}, include_inactive. Returns the total and whether more "
        "rows remain. For joins or aggregates use query."
    ),
    "revert_change": (
        "Undo an applied change by applying its stored reverse (approvers only). Changes that dropped something "
        "cannot be undone this way; the error says so."
    ),
    "recheck_change": "Run the gate again on a stale change, after the schema or the rules moved.",
}


class ToolFailure(Exception):
    """An error the AI should see and can act on."""


def allowed(user, tool: str) -> bool:
    need = TOOL_ROLES.get(tool)
    return need is not None and user is not None and user.rank >= RANK[need]


def clamp(value, default: int, maximum: int) -> int:
    """A list size an AI asked for, made safe: nonsense and anything below 1 give `default`, too much gives `maximum`."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return min(value, maximum)


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


def get_settings(user) -> dict:
    """The quality settings, every rule (including switched-off ones) and how to change them."""
    return {
        "instructions": (
            "These are the important knobs. To change any of them call propose_config_change; the change waits for an "
            "approver in the approval queue, so tell the user it is pending and who can approve it."
        ),
        "settings": [{k: t[k] for k in ("key", "label", "help", "kind", "min", "max", "default", "value", "overridden", "options") if k in t}
                     for t in tunables.describe()],
        "rules": [{k: r[k] for k in ("name", "type", "severity", "enabled", "description", "params")} for r in rule_services.active_rules()],
    }


def list_users(user) -> dict:
    from accounts.models import User

    return {"users": [{"email": u.email, "name": f"{u.first_name} {u.last_name}".strip(), "role": u.role, "active": u.is_active}
                      for u in User.objects.order_by("email")]}


def propose_config_change(user, operations: list, prompt: str = "", *, source: str = "mcp",
                          conversation_id: int | None = None) -> dict:
    from approvals import config_ops

    if not config_ops.is_config(operations):
        raise ToolFailure(f"operations must all be configuration operations: {sorted(config_ops.CONFIG_OPS)}")
    return propose_change(user, operations, prompt, source=source, conversation_id=conversation_id)


def list_schemas(user) -> dict:
    """The databases (Postgres schemas) that can be read and changed, `master` first."""
    return {"schemas": introspect.managed_schemas()}


def list_tables(user, schema: str = "master") -> dict:
    try:
        return {"tables": introspect.list_tables(schema)}
    except introspect.UnknownSchema as exc:
        raise ToolFailure(f"{exc} Call list_schemas to see the databases.") from exc


def describe_table(user, table: str, schema: str = "master") -> dict:
    try:
        d = introspect.describe_table(table, schema)
    except introspect.UnknownSchema as exc:
        raise ToolFailure(f"{exc} Call list_schemas to see the databases.") from exc
    if d is None:
        raise ToolFailure(f"table {table!r} does not exist in {schema!r}. Call list_tables to see what exists.")
    return d


def run_query(user, sql: str, limit: int | None = None, schema: str = "master") -> dict:
    limits = get_config()["limits"]
    cap = limits["maxQueryRows"]
    try:
        return query.run_select(sql, limit=min(limit or cap, cap), timeout_ms=limits["queryTimeoutMs"], schema=schema)
    except (query.QueryError, introspect.UnknownSchema) as exc:
        raise ToolFailure(str(exc)) from exc


def read_rows(user, table: str, schema: str = "master", offset: int = 0, limit: int | None = None,
              sort: str | None = None, direction: str = "asc", filters: dict | None = None,
              include_inactive: bool = False) -> dict:
    """One page of one table. `truncated` says whether rows remain after this page."""
    from schema import rows as rows_mod

    if filters is not None and not isinstance(filters, dict):
        raise ToolFailure("filters must be an object of {column: text to look for}, for example {\"name_en\": \"buri\"}.")
    start = offset if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0 else 0
    try:
        out = rows_mod.read_rows(table, offset=start, limit=clamp(limit, 50, rows_mod.MAX_PAGE), sort=sort or None,
                                 direction=direction, filters=filters, include_inactive=bool(include_inactive),
                                 schema=schema)
    except introspect.UnknownSchema as exc:
        raise ToolFailure(f"{exc} Call list_schemas to see the databases.") from exc
    except rows_mod.RowsError as exc:
        if exc.status == 404:
            raise ToolFailure(f"table {table!r} does not exist in {schema!r}. Call list_tables to see what exists.") from exc
        hint = " Call describe_table to see the columns." if "unknown column" in str(exc) else ""
        raise ToolFailure(f"{exc}.{hint}") from exc
    return {**out, "truncated": out["offset"] + len(out["rows"]) < out["total"]}


def _next_step(user, change: Change) -> str:
    if change.status == "rejected" and change.decided_by_id is None:
        if change.kind == "config":
            return "This configuration change is not valid. Read 'check.errors', fix the operations and call propose_config_change again."
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
        return f"The schema or rules changed since this was checked. Call recheck_change with change_id={change.pk} to run the gate again."
    if change.status == "failed":
        return "The database refused the change and nothing was changed. See 'error'."
    return ""


def _result(user, change: Change) -> dict:
    return {**service.serialize(change), "next": _next_step(user, change)}


def search_schema(user, query: str, limit: int | None = None, schema: str = "master") -> dict:
    from search import service as search_service
    from search.embedder import SearchError

    try:
        return search_service.search(query, limit, None if schema == "*" else schema)  # "*" = every schema
    except (SearchError, introspect.UnknownSchema) as exc:
        raise ToolFailure(str(exc)) from exc


def propose_change(user, operations: list, prompt: str = "", *, source: str = "mcp",
                   conversation_id: int | None = None, schema: str = "master") -> dict:
    try:
        change = service.propose(user, operations, source=source, prompt=prompt[:10_000],
                                 conversation_id=conversation_id, schema=schema)
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


def list_changes(user, status: str | None = None, kind: str | None = None, schema: str | None = None,
                 mine: bool = False, limit: int | None = None) -> dict:
    """History of proposed changes, newest first. Summaries only; get_change has the diff."""
    if schema is not None and not isinstance(schema, str):
        return {"changes": [], "truncated": False}
    items, more = service.list_changes(user, status=status if isinstance(status, str) else None,
                                       kind=kind if isinstance(kind, str) else None, schema=schema,
                                       mine=bool(mine), limit=clamp(limit, 20, 100))
    return {"changes": [service.serialize(c, detail=False) for c in items], "truncated": more}


def get_audit_log(user, action: str | None = None, actor: str | None = None, limit: int | None = None) -> dict:
    """Who did what and when (approvers only)."""
    from audit.models import as_dict, entries

    items, more = entries(action=action if isinstance(action, str) else None,
                          actor=actor if isinstance(actor, str) else None, limit=clamp(limit, 50, 200))
    return {"entries": [as_dict(a) for a in items], "truncated": more}


def revert_change(user, change_id: int) -> dict:
    """Undo an applied change by applying its stored reverse operations."""
    try:
        return _result(user, service.revert(user, change_id))
    except service.ChangeError as exc:
        raise ToolFailure(exc.message) from exc


def recheck_change(user, change_id: int) -> dict:
    """Run the gate again on a stale change."""
    try:
        return _result(user, service.recheck(user, change_id))
    except service.ChangeError as exc:
        raise ToolFailure(exc.message) from exc


def list_pending(user) -> dict:
    items = service.pending_for(user).select_related("requester", "decided_by")[:50]
    return {"pending": [service.serialize(c, detail=False) for c in items]}





def submit_script(user, name: str, content: str, mapping: dict | None = None, schema: str = "master") -> dict:
    """Upload + validate inline (<= maxInlineScriptBytes). Larger scripts: upload via API/UI, pass script_id."""
    from config.siconfig import get_config
    try:
        introspect.require_managed(schema)  # before uploading anything
    except introspect.UnknownSchema as exc:
        raise ToolFailure(f"{exc} Call list_schemas to see the databases.") from exc
    data = content.encode("utf-8")
    if len(data) > get_config()["limits"]["maxInlineScriptBytes"]:
        raise ToolFailure(
            f"inline script is {len(data)} bytes; the inline cap is {get_config()['limits']['maxInlineScriptBytes']}. "
            "Upload the file via the web UI or REST API, then call validate_script/get_script with its script_id.")
    try:
        script = script_service.upload(user, name, data)
        report = script_service.validate(user, script.pk, mapping, schema)
    except (storage.ScriptError, introspect.UnknownSchema) as exc:
        raise ToolFailure(str(exc)) from exc
    out = {"script_id": script.pk, "report": _trim_report(report)}
    if report.get("ok"):
        try:
            change = script_service.submit(user, script.pk)
            out["change_id"] = change.pk
            out["next"] = _next_step(user, change)
        except storage.ScriptError as exc:
            out["submit_error"] = str(exc)
    return out


def validate_script(user, script_id: int, mapping: dict | None = None, schema: str = "master") -> dict:
    try:
        report = script_service.validate(user, script_id, mapping, schema)
    except (storage.ScriptError, introspect.UnknownSchema) as exc:
        raise ToolFailure(str(exc)) from exc
    return {"script_id": script_id, "report": _trim_report(report)}


def get_script(user, script_id: int) -> dict:
    from scripts.models import Script
    s = Script.objects.filter(pk=script_id).first()
    if s is None:
        raise ToolFailure(f"script #{script_id} does not exist")
    return {"id": s.pk, "name": s.name, "size": s.size, "status": s.status,
            "change_id": s.change_id, "report": _trim_report(s.report or {}) if s.report else None}


def _trim_report(report: dict) -> dict:
    out = dict(report)
    out["errors"] = report.get("errors", [])[:20]
    for t in out.get("tables", []):
        t["sample_orphans"] = t.get("sample_orphans", [])[:10]
    return out


IMPL = {
    "get_rules": get_rules,
    "list_schemas": list_schemas,
    "list_tables": list_tables,
    "describe_table": describe_table,
    "query": run_query,
    "search_schema": search_schema,
    "propose_change": propose_change,
    "get_settings": get_settings,
    "propose_config_change": propose_config_change,
    "list_users": list_users,
    "get_change": get_change,
    "approve_change": approve_change,
    "reject_change": reject_change,
    "list_pending": list_pending,
    "submit_script": submit_script,
    "validate_script": validate_script,
    "get_script": get_script,
    "list_changes": list_changes,
    "get_audit_log": get_audit_log,
    "read_rows": read_rows,
    "revert_change": revert_change,
    "recheck_change": recheck_change,
}
