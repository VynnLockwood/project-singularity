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
    "export_schema": "viewer",     # with data: editor, checked in the tool like the REST endpoint
    "find_duplicates": "editor",
    "list_duplicates": "viewer",
    "get_duplicate": "viewer",
    "merge_duplicates": "editor",
    "dismiss_duplicate": "editor",
    "revert_change": "approver",
    "reindex_search": "approver",
    "recheck_change": "editor",
    "get_audit_log": "approver",   # audit details can hold other people's prompts and emails
}

RANK = {"viewer": 0, "editor": 1, "approver": 2}

# What each newer tool is for, in the words an AI client reads. One text, used by both the MCP
# server (mcp_server/app.py) and the web chat (chat/toolspecs.py), so the two cannot drift apart.
# The operations the gate understands, as one text for every client that proposes changes (the MCP server's
# propose_change and the web chat's). Edit it here only.
OPERATIONS_HELP = (
    "Each operation is an object with an 'op': "
    "create_table{name,columns[{name,type,nullable?,unique?,default?,comment?,references?{table,column?}}],comment?}, "
    "add_column{table,column}, rename_table{table,new_name}, rename_column{table,column,new_name}, "
    "alter_column_type{table,column,type}, set_column_comment{table,column,comment}, add_index{table,columns,unique?}, "
    "add_unique{table,columns}, add_foreign_key{table,column,ref_table,ref_column?,on_delete?(restrict|cascade|set null|no action)}, "
    "add_check{table,name,expression}, set_table_comment{table,comment}, "
    "set_column_default{table,column,default}, drop_column_default{table,column}, "
    "set_not_null{table,column} (refused while the column holds NULLs), drop_not_null{table,column}, "
    "create_view{name,query,comment?} (one read-only SELECT over this database's tables), drop_view{name,reason}, "
    "create_enum{name,values[]} (then use the name as a column type), add_enum_value{name,value}, drop_enum{name,reason}, "
    "drop_column{table,column,reason}, drop_table{table,reason}, drop_index{name,reason}, drop_constraint{table,name,reason}, "
    "insert_rows{table,rows[]}, update_rows{table,where{},set{}}, deactivate_rows{table,where{}}, "
    "delete_rows{table,where{},reason} (real deletion; prefer deactivate_rows), "
    "upsert_rows{table,key[],columns[],rows[]} (insert, or overwrite 'columns' when the key exists; the key must be a unique key). "
    "Database-level operations: create_schema{name,comment?}, rename_schema{schema,new_name}, drop_schema{schema,reason}, "
    "move_table{table,from_schema,to_schema}. These go alone, in their own change; a 'database' is a Postgres schema. "
    "Every other operation acts on the `schema` argument (default master). "
    "(bulk_insert exists but is produced by script intake only; never write it yourself, the gate refuses it.) "
    "Types: text, integer, bigint, smallint, boolean, date, timestamptz, timestamp, uuid, jsonb, real, "
    "double precision, numeric, numeric(p,s), varchar(n), or the name of an enum made with create_enum. "
    "Audit columns are added automatically."
)

# One example per newer operation, returned by get_rules so a client can copy the exact shape.
OPERATION_EXAMPLES = {
    "set_column_default": {"op": "set_column_default", "table": "customer", "column": "status", "default": "new"},
    "drop_column_default": {"op": "drop_column_default", "table": "customer", "column": "status"},
    "set_not_null": {"op": "set_not_null", "table": "customer", "column": "name"},
    "drop_not_null": {"op": "drop_not_null", "table": "customer", "column": "name"},
    "create_view": {"op": "create_view", "name": "active_customer", "query": "SELECT id, name FROM customer WHERE is_active",
                    "comment": "customers still active"},
    "drop_view": {"op": "drop_view", "name": "active_customer", "reason": "replaced by a report"},
    "create_enum": {"op": "create_enum", "name": "customer_status", "values": ["new", "active", "closed"]},
    "add_enum_value": {"op": "add_enum_value", "name": "customer_status", "value": "paused"},
    "drop_enum": {"op": "drop_enum", "name": "customer_status", "reason": "no column uses it"},
    "delete_rows": {"op": "delete_rows", "table": "customer", "where": {"code": ["C-9"]}, "reason": "created by mistake"},
    "upsert_rows": {"op": "upsert_rows", "table": "customer", "key": ["code"], "columns": ["name"],
                    "rows": [{"code": "C-1", "name": "Acme"}]},
    "move_table": {"op": "move_table", "table": "customer", "from_schema": "master", "to_schema": "crm"},
}


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
    "export_schema": (
        "A database (schema) as Postgres SQL: the structure, and the data too with include_data=true (editors). "
        "Returned inline up to a size limit; when 'truncated' is true, tell the user to download the full file in "
        "the web app."
    ),
    "find_duplicates": (
        "Scan one table for records that look like the same thing (fuzzy match on its text columns, or on the "
        "'fields' you name). Returns the scan summary and the open groups."
    ),
    "list_duplicates": (
        "Duplicate groups already found, by table and status: open (the default), dismissed, merge_pending or merged. "
        "Pass status=null for all."
    ),
    "get_duplicate": "The records of one duplicate group side by side, and the tables that reference them.",
    "merge_duplicates": (
        "Propose merging a group into one surviving record: references move to the survivor and the other records "
        "are deactivated. This is a data change and goes through approval. Show the user the records first "
        "(get_duplicate) and ask which one to keep."
    ),
    "dismiss_duplicate": "Mark a group as not duplicates, so a scan does not bring it back.",
    "revert_change": (
        "Undo an applied change by applying its stored reverse (approvers only). Changes that dropped something "
        "cannot be undone this way; the error says so."
    ),
    "reindex_search": (
        "Refresh the schema search index (approvers). It runs by itself after schema changes; use rebuild=true only "
        "when search results look wrong."
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
        "operations": OPERATION_EXAMPLES,
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
        return {"tables": introspect.list_tables(schema), "views": introspect.list_views(schema),
                "enums": introspect.list_enums(schema)}
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


def reindex_search(user, rebuild: bool = False) -> dict:
    """Refresh the schema search index. It also runs by itself after every applied schema change."""
    from audit.models import record
    from search import indexer
    from search.embedder import SearchError

    try:
        result = indexer.reindex(rebuild=bool(rebuild))
    except SearchError as exc:
        raise ToolFailure(f"{exc}. The index was left as it was; try again later.") from exc
    record(user, "search.reindexed", details={**result, "via": "mcp"})
    return result


def cut_sql(text: bytes, cap: int) -> tuple[bytes, bool]:
    """Fit exported SQL under `cap` bytes without ever returning half a statement.

    Returns the text and whether it was cut. A cut lands after the last complete statement at or
    before the cap: a line ending in `;` outside a COPY block and outside a quoted string (a table
    comment may hold `;` and line breaks), or the `\\.` line that closes a COPY block. A COPY header
    and its rows count as one statement, so data is never cut in the middle.
    """
    if len(text) <= cap:
        return text, False
    safe = pos = 0
    in_copy = in_string = False
    for line in text[:cap].splitlines(keepends=True):
        pos += len(line)
        if not line.endswith(b"\n"):
            break                                   # the cap fell inside this line
        body = line.rstrip(b"\r\n")
        if in_copy:
            if body == b"\\.":
                in_copy, safe = False, pos
            continue
        opened_in_string = in_string
        if body.count(b"'") % 2:                    # a doubled '' inside a string keeps the count even
            in_string = not in_string
        if in_string:
            continue
        if not opened_in_string and body.startswith(b"COPY ") and body.endswith(b"FROM stdin;"):
            in_copy = True
        elif body.endswith(b";"):
            safe = pos
    return text[:safe], True


def export_schema(user, schema: str = "master", include_data: bool = False, *, max_bytes: int | None = None) -> dict:
    """A database as Postgres SQL, inline up to limits.maxInlineExportBytes (or `max_bytes`, if that is smaller)."""
    from audit.models import record
    from schema.dump_model import ExportError
    from schema.export import open_export

    if include_data and not user.can_edit:
        raise ToolFailure("exporting data needs the editor role. Call export_schema without include_data for the structure only.")
    limits = get_config()["limits"]
    cap = int(limits["maxInlineExportBytes"])
    if max_bytes is not None:
        cap = max(0, min(cap, int(max_bytes)))
    try:
        ex = open_export(schema if isinstance(schema, str) else "", data=bool(include_data), cap=limits["maxExportRows"])
    except ExportError as exc:
        hint = " Call list_schemas to see the databases." if str(exc).lower().startswith("unknown schema") else ""
        raise ToolFailure(f"{exc}{hint}") from exc
    try:
        buf = bytearray()
        for chunk in ex.chunks():
            buf += chunk
            if len(buf) > cap:      # one byte over is enough to know it must be cut
                break
        name, counts = ex.filename(), ex.counts
    finally:
        ex.close()
    text, truncated = cut_sql(bytes(buf), cap)
    record(user, "schema.export", schema, {"schema": schema, "data": bool(include_data), "counts": counts,
                                           "via": "mcp", "truncated": truncated})
    sql_text = text.decode("utf-8", errors="replace")
    note = "Cut at the inline limit. Download the full file in the web app: Database, then Download SQL." if truncated else ""
    return {"filename": name, "sql": sql_text, "bytes": len(text), "truncated": truncated, "note": note}


def _dup(fn):
    """Run a duplicates call, turning its errors into ones the AI can act on."""
    from duplicates import service as dup

    try:
        return fn(dup)
    except introspect.UnknownSchema as exc:
        raise ToolFailure(f"{exc} Call list_schemas to see the databases.") from exc
    except dup.DuplicateError as exc:
        text = str(exc).rstrip(".")
        if text.lower().startswith("unknown schema"):
            hint = " Call list_schemas to see the databases."
        elif exc.status == 404 and "group" in text:
            hint = " Call list_duplicates to see the groups."
        elif exc.status == 404:
            hint = " Call list_tables to see what exists."
        else:
            hint = ""
        raise ToolFailure(f"{text}.{hint}") from exc


def find_duplicates(user, table: str, schema: str = "master", fields: list | None = None,
                    threshold: float | None = None) -> dict:
    """Scan one table for records that look like the same thing; returns the scan summary and the groups."""
    from audit.models import record

    if fields is not None and (not isinstance(fields, list) or not all(isinstance(f, str) for f in fields)):
        raise ToolFailure("fields must be a list of column names, for example [\"name\"]. Leave it out to use every text column.")
    result = _dup(lambda dup: dup.scan(str(table), user, fields=fields, threshold=threshold, schema=schema))
    record(user, "duplicates.scanned", target=result["table"], details=result)
    groups = _dup(lambda dup: dup.list_groups(schema=schema, table=result["table"], status="open"))
    return {**result, "groups_found": groups[:20], "truncated": result["truncated"] or len(groups) > 20}


def list_duplicates(user, table: str | None = None, schema: str = "master", status: str | None = "open",
                    limit: int | None = None) -> dict:
    cap = clamp(limit, 20, 100)
    groups = _dup(lambda dup: dup.list_groups(schema=schema, table=table or None, status=status or None))
    return {"groups": groups[:cap], "truncated": len(groups) > cap}


def get_duplicate(user, group_id: int) -> dict:
    from duplicates import service as dup

    g = _dup(lambda d: d.get_group(group_id))
    try:
        return dup.detail(g)
    except Exception as exc:  # the table was dropped or changed shape since the scan
        raise ToolFailure(
            f"the records of group #{g.pk} can no longer be read (table {g.table_name!r} changed or is gone). "
            f"Call dismiss_duplicate with group_id={g.pk}, or find_duplicates to scan again.") from exc


def merge_duplicates(user, group_id: int, survivor_id: int, *, source: str = "mcp",
                     conversation_id: int | None = None) -> dict:
    from duplicates import service as dup

    if not isinstance(survivor_id, int) or isinstance(survivor_id, bool):
        raise ToolFailure("survivor_id must be the id of the record to keep (one of the group's memberIds).")
    g = _dup(lambda d: d.get_group(group_id))
    if dup.state(g) == "dismissed":
        raise ToolFailure(
            f"group #{g.pk} was dismissed as not duplicates, so it cannot be merged. If that was a mistake, the "
            "user can reopen it in the web app (Duplicates); find_duplicates will not bring a dismissed group back.")
    try:
        change = dup.propose_merge(g, survivor_id, user, source=source, conversation_id=conversation_id)
    except dup.DuplicateError as exc:
        if exc.status == 404:   # the table or its key is gone since the scan
            raise ToolFailure(
                f"the records of group #{g.pk} can no longer be read (table {g.table_name!r} changed or is gone). "
                f"Call dismiss_duplicate with group_id={g.pk}, or find_duplicates to scan again.") from exc
        raise ToolFailure(f"{str(exc).rstrip('.')}.") from exc
    return {"group": dup.summary(g), "change": _result(user, change)}


def dismiss_duplicate(user, group_id: int) -> dict:
    return _dup(lambda d: d.dismiss(d.get_group(group_id), user))


def _next_step(user, change: Change) -> str:
    if change.status == "rejected" and change.decided_by_id is None:
        if change.kind == "config":
            return "This configuration change is not valid. Read 'check.errors', fix the operations and call propose_config_change again."
        return "The gate blocked this change. Fix the problems in 'check' (use the suggestions) and call propose_change again."
    if change.status == "pending":
        if user.can_approve and service.can_decide(user, change):   # approve_change is an approver-only tool
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
    "export_schema": export_schema,
    "find_duplicates": find_duplicates,
    "list_duplicates": list_duplicates,
    "get_duplicate": get_duplicate,
    "merge_duplicates": merge_duplicates,
    "dismiss_duplicate": dismiss_duplicate,
    "revert_change": revert_change,
    "reindex_search": reindex_search,
    "recheck_change": recheck_change,
}
