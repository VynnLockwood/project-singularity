# MCP Plan A (Reach) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give an MCP client (and the web chat assistant) a tool for every feature the web app already has: change history, audit log, revert, re-check, paged rows, duplicates, schema export, search reindex.

**Architecture:** Each new tool is a plain function in `mcp_server/tools.py` that calls an existing service. Where a REST view holds logic the service lacks (change filters, audit filters, duplicate listing and dismissal), that logic moves into the service and the view calls it too. `mcp_server/app.py` and `chat/toolspecs.py` only register and describe.

**Tech Stack:** Django 5, psycopg 3, the `mcp` Python SDK (v2), pytest with a real Postgres 16 and the real restricted roles.

**Spec:** `docs/superpowers/specs/2026-11-10-mcp-full-coverage-design.md` (sections 1, 2, 5, 6, 7)

## Global Constraints

- Working directory for every command: `backend/`. Branch: `mcp/full-coverage`. Commits authored as the repository owner (local git config); no co-author trailers.
- Tests need a Postgres superuser: `TEST_PG_ADMIN_USER=postgres DB_HOST=localhost DB_APP_USER=postgres DB_APP_PASSWORD=<pw> DJANGO_DEBUG=1 pytest -q`. The Rust engine wheel must be installed for the duplicates tests (`cd ../engine && maturin build --release --out /tmp/wheels && pip install /tmp/wheels/*.whl`). The whole suite stays green after every task.
- No new dependencies. No database migrations in this plan.
- A tool is a function `(user, ...) -> dict` in `mcp_server/tools.py`, listed in `TOOL_ROLES` and `IMPL`. Errors the AI can act on are raised as `ToolFailure` with what went wrong and which tool to call next. No stack traces and no raw driver text beyond the primary message.
- Every list result is capped and carries `"truncated": true|false`.
- An unknown schema always produces the existing message form: `f"{exc} Call list_schemas to see the databases."`.
- Role rules come from the services. The tool layer adds only `TOOL_ROLES`.
- Minimum roles are exactly those in spec section 2: `list_changes` viewer, `get_audit_log` approver, `revert_change` approver, `recheck_change` editor, `read_rows` viewer, `find_duplicates` editor, `list_duplicates` viewer, `get_duplicate` viewer, `merge_duplicates` editor, `dismiss_duplicate` editor, `export_schema` viewer (editor when `include_data` is true), `reindex_search` approver.
- The web chat assistant gets every new tool. It still has no `approve_change` and no `reject_change`.
- REST responses do not change shape.

## Review Focus

1. **Bad list arguments.** `limit` of 0, a negative number, a huge number, or a status that does not exist (`list_changes(status="bogus")`): the result is clamped or empty, never an exception. (Tests in Task 1 and Task 3.)
2. **Revert of something that cannot be reverted.** A change that dropped a table, a change that is still pending, a change already reverted: `ToolFailure` with the service's reason, not a crash. (Test in Task 2.)
3. **Merge with a wrong survivor.** `survivor_id` not in the group, a group already merged or dismissed, a group whose table was dropped: `ToolFailure` that says what to do. (Test in Task 4.)
4. **Export at the edges.** Empty database; a result exactly at the cap (not truncated); one byte over (truncated, cut at a statement end, says where to get the rest); a viewer asking for data (refused). (Test in Task 5.)
5. **Unknown names everywhere.** Unknown schema, table, change id, group id on each new tool: a message that names the tool listing valid values. (Tests in every task.)

---

### Task 1: Registry guard, change history and audit log

**Files:**
- Modify: `approvals/service.py`, `approvals/views.py`, `audit/models.py`, `audit/views.py`, `mcp_server/tools.py`, `mcp_server/app.py`, `chat/toolspecs.py`
- Create: `tests/test_mcp_tools_registry.py`, `tests/test_mcp_history.py`

**Interfaces:**
- Produces:
  - `approvals.service.list_changes(user, *, status: str | None = None, kind: str | None = None, schema: str | None = None, mine: bool = False, limit: int = 50) -> tuple[list[Change], bool]` (changes newest first, and whether more exist). `schema` is validated with `introspect.require_managed` when given.
  - `audit.models.entries(*, action: str | None = None, actor: str | None = None, limit: int = 100) -> tuple[list[AuditLog], bool]` (`actor` matches `actor_email` exactly, case-insensitive).
  - `tools.clamp(value, default: int, maximum: int) -> int` (None, non-numbers, and values below 1 give `default`; values above `maximum` give `maximum`).
  - `tools.list_changes(user, status=None, kind=None, schema=None, mine=False, limit=None) -> {"changes": [...], "truncated": bool}` (default 20, max 100, `serialize(c, detail=False)`).
  - `tools.get_audit_log(user, action=None, actor=None, limit=None) -> {"entries": [{"id","at","actor","action","target","details"}], "truncated": bool}` (default 50, max 200).

- [ ] **Step 1: Write the failing registry test** `tests/test_mcp_tools_registry.py`

```python
import asyncio

from chat import toolspecs
from mcp_server import tools

NOT_IN_CHAT = {"approve_change", "reject_change"}   # the person decides in the web app, never the model


def test_every_tool_has_a_role_and_an_implementation():
    assert set(tools.TOOL_ROLES) == set(tools.IMPL)


def test_every_tool_is_registered_with_the_mcp_server():
    from mcp_server.app import mcp
    registered = {t.name for t in asyncio.run(super(type(mcp), mcp).list_tools())}
    assert registered == set(tools.TOOL_ROLES)


def test_the_web_chat_has_every_tool_except_deciding():
    assert set(toolspecs.SPECS) == set(tools.TOOL_ROLES) - NOT_IN_CHAT


def test_clamp():
    assert tools.clamp(None, 20, 100) == 20
    assert tools.clamp(0, 20, 100) == 20
    assert tools.clamp(-5, 20, 100) == 20
    assert tools.clamp("abc", 20, 100) == 20
    assert tools.clamp(7, 20, 100) == 7
    assert tools.clamp(10**9, 20, 100) == 100
```

- [ ] **Step 2:** Run `pytest -q tests/test_mcp_tools_registry.py`. Expected: `test_clamp` fails (no attribute `clamp`); the other three pass today. If the chat test fails today, note the missing names in the commit message and add their `SPECS` entries in this task.
- [ ] **Step 3: Write the failing tests** `tests/test_mcp_history.py` (module marks: `pytest.mark.django_db`; users made as in `tests/test_script_mcp.py`; changes made with `Change.objects.create(...)` directly, since no master database is needed):
  - `test_list_changes_newest_first_and_capped`: 25 changes, default call returns 20 with `truncated` true and ids descending; `limit=100` returns 25 with `truncated` false.
  - `test_list_changes_filters`: `status="applied"`, `kind="data"`, `mine=True` each return only matching rows; combined filters intersect.
  - `test_list_changes_bad_arguments_do_not_raise`: `status="bogus"` returns an empty list; `limit=0`, `limit=-3`, `limit=10**9` return at most 100 rows.
  - `test_list_changes_unknown_schema`: raises `ToolFailure` whose text contains `list_schemas`.
  - `test_audit_log_is_for_approvers`: `tools.allowed(editor, "get_audit_log")` is false, `tools.allowed(approver, "get_audit_log")` is true.
  - `test_audit_log_filters_and_caps`: 60 entries of two actions and two actors; default returns 50 with `truncated` true; `action=` and `actor=` (different letter case) filter; entry keys are exactly `id, at, actor, action, target, details`.
  - `test_rest_changes_and_audit_still_work`: `GET /api/changes?status=applied&mine=1` and `GET /api/audit?action=x` return the same items as before the refactor (use the Django test client and the existing login helper from `tests/test_api.py`).
- [ ] **Step 4:** Run `pytest -q tests/test_mcp_history.py`. Expected: FAIL (no `list_changes` in `tools`).
- [ ] **Step 5:** Implement `service.list_changes` and `audit.models.entries` by moving the query code out of `approvals/views.py::changes` (GET branch) and `audit/views.py::audit_log`; both views call the new functions and keep their response shape. Fetch `limit + 1` rows to compute the truncated flag. Implement `clamp`, `list_changes`, `get_audit_log` in `tools.py`; add to `TOOL_ROLES` and `IMPL`.
- [ ] **Step 6:** Register both in `mcp_server/app.py` (same pattern as `list_pending`) with descriptions:
  - `list_changes`: "History of proposed changes, newest first: pending, applied, rejected, failed or stale. Filter by status, kind (schema, data, config), schema, or mine=true for your own. Summaries only; call get_change for the diff."
  - `get_audit_log`: "Who did what and when (approvers only): sign-ins, proposals, decisions, exports, settings changes. Filter by action or actor email."
  Add matching `SPECS` entries in `chat/toolspecs.py`.
- [ ] **Step 7:** Run the whole suite. Expected: PASS.
- [ ] **Step 8:** Commit: `feat(mcp): change history and audit log tools`.

### Task 2: Revert and re-check

**Files:**
- Modify: `mcp_server/tools.py`, `mcp_server/app.py`, `chat/toolspecs.py`
- Create: `tests/test_mcp_change_actions.py`

**Interfaces:**
- Consumes: `service.revert(user, change_id) -> Change`, `service.recheck(user, change_id) -> Change`, `tools._result(user, change)`.
- Produces: `tools.revert_change(user, change_id: int) -> dict`, `tools.recheck_change(user, change_id: int) -> dict`; both return `_result(user, change)`.

- [ ] **Step 1: Write the failing tests** (marks: `django_db(transaction=True)`, `usefixtures("master")`; build state by calling `tools.propose_change` then `tools.approve_change` as the approver):
  - `test_revert_undoes_an_applied_change`: create a table through the tools, revert it; the returned change has `status == "applied"` and `tools.list_tables` no longer lists the table; `next` says done.
  - `test_revert_refuses_what_cannot_be_undone`: (a) a change that is still pending, (b) a change that dropped a table, (c) reverting twice, (d) `change_id=999999`: each raises `ToolFailure` whose message is the service's message.
  - `test_revert_is_for_approvers`: `tools.allowed(editor, "revert_change")` is false.
  - `test_recheck_a_stale_change`: propose a change, alter the schema with a second applied change so the first becomes stale on approve, call `recheck_change`; the result is `pending` again or `rejected` with the gate's reason, never an exception.
  - `test_recheck_unknown_change`: raises `ToolFailure`.
- [ ] **Step 2:** Run. Expected: FAIL (no attribute).
- [ ] **Step 3:** Implement both (wrap `service.ChangeError` into `ToolFailure(exc.message)`), add to `TOOL_ROLES`, `IMPL`, `app.py`, `SPECS`. Descriptions:
  - `revert_change`: "Undo an applied change by applying its stored reverse (approvers only). Changes that dropped something, and raw SQL, cannot be undone this way; the error says so."
  - `recheck_change`: "Run the gate again on a stale change, after the schema or the rules moved."
- [ ] **Step 4:** Run the whole suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(mcp): revert and re-check tools`.

### Task 3: Paged rows

**Files:**
- Modify: `mcp_server/tools.py`, `mcp_server/app.py`, `chat/toolspecs.py`
- Create: `tests/test_mcp_read_rows.py`

**Interfaces:**
- Consumes: `schema.rows.read_rows(table, *, offset, limit, sort, direction, filters, include_inactive, schema) -> dict`, `rows.RowsError`.
- Produces: `tools.read_rows(user, table: str, schema: str = "master", offset: int = 0, limit: int | None = None, sort: str | None = None, direction: str = "asc", filters: dict | None = None, include_inactive: bool = False) -> dict` returning what `rows.read_rows` returns plus `"truncated": offset + len(rows) < total`.

- [ ] **Step 1: Write the failing tests** (marks as Task 2; make a `province` table with 30 rows through the tools):
  - `test_first_page_and_total`: default call returns 50-capped page (here all 30), `total == 30`, `truncated` false; `limit=10` returns 10 with `truncated` true.
  - `test_offset_sort_and_filter`: `offset=10, limit=10, sort="name_en", direction="desc"` returns the right slice; `filters={"name_en": "ban"}` returns only matching rows.
  - `test_limits_are_clamped`: `limit=10**6` returns at most 200 rows; `limit=0` and `offset=-4` behave as default and 0.
  - `test_unknown_table_schema_column`: unknown table, unknown schema (message contains `list_schemas`), `sort="nope"`, `filters={"nope": "x"}`, `filters="text"`: each raises `ToolFailure`.
  - `test_a_viewer_may_read`: `tools.allowed(viewer, "read_rows")` is true.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement (`limit` through `clamp(limit, 50, 200)`, `offset` floored at 0, `filters` must be a dict or None), register, add `SPECS`. Description: "Read rows of one table, a page at a time, without writing SQL: offset, limit (max 200), sort, direction, filters {column: text contained}, include_inactive. Returns the total. For joins or aggregates use query."
- [ ] **Step 4:** Run the whole suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(mcp): read_rows tool`.

### Task 4: Duplicates

**Files:**
- Modify: `duplicates/service.py`, `duplicates/views.py`, `mcp_server/tools.py`, `mcp_server/app.py`, `chat/toolspecs.py`
- Create: `tests/test_mcp_duplicates.py`

**Interfaces:**
- Produces in `duplicates/service.py`:
  - `list_groups(*, schema: str | None = "master", table: str | None = None, status: str | None = None) -> list[dict]` (summaries; `schema=None` means all; validates a given schema with `introspect.require_managed`).
  - `get_group(group_id: int) -> DuplicateGroup` (raises `DuplicateError("duplicate group #N does not exist", 404)`).
  - `dismiss(group: DuplicateGroup, user) -> dict` (sets status, `decided_by`, records `duplicates.dismissed`, returns the summary).
  The three REST views (`groups`, `dismiss`, and the lookups in `group_detail`, `reopen`, `merge`) call these.
- Produces in `tools.py`:
  - `find_duplicates(user, table: str, schema: str = "master", fields: list[str] | None = None, threshold: float | None = None) -> dict` (the scan result, plus `"groups_found": [summary, ...]` for this table capped at 20, plus `truncated`); records `duplicates.scanned` as the view does.
  - `list_duplicates(user, table: str | None = None, schema: str = "master", status: str | None = "open", limit: int | None = None) -> {"groups": [...], "truncated": bool}` (default 20, max 100).
  - `get_duplicate(user, group_id: int) -> dict` (`service.detail`).
  - `merge_duplicates(user, group_id: int, survivor_id: int) -> dict` (`{"group": summary, "change": _result(user, change)}`).
  - `dismiss_duplicate(user, group_id: int) -> dict` (summary).

- [ ] **Step 1: Write the failing tests** (marks as Task 2; follow the data setup in `tests/test_duplicates.py`: a `supplier` table with two near-identical names and one different):
  - `test_find_lists_the_group`: `find_duplicates` returns `groups >= 1` and `groups_found[0]["memberIds"]` holds the two alike rows; `list_duplicates(table="supplier")` returns the same group.
  - `test_get_shows_records_side_by_side`: `get_duplicate` returns `members` (2) and `primaryKey`.
  - `test_merge_proposes_a_data_change`: `merge_duplicates(group, survivor)` returns a change with `kind == "data"`, `status == "pending"`, and a `next` hint; after `approve_change` the group's status is `merged`.
  - `test_merge_refuses_bad_input`: survivor not in the group; group already merged; `group_id=999999`; `survivor_id=True`: each raises `ToolFailure`.
  - `test_dismiss_and_list_by_status`: after `dismiss_duplicate`, `list_duplicates(status="open")` is empty and `status="dismissed"` has it; an audit entry `duplicates.dismissed` exists.
  - `test_group_whose_table_was_dropped`: drop the table through the tools, then `get_duplicate` raises `ToolFailure` telling the AI to run `find_duplicates` again or dismiss the group.
  - `test_unknown_table_or_schema`: `find_duplicates(table="nope")` and `schema="nope"` raise `ToolFailure`.
  - `test_roles`: viewer may `list_duplicates` and `get_duplicate`, not `find_duplicates`, `merge_duplicates`, `dismiss_duplicate`.
  - `test_rest_duplicates_unchanged`: `GET /api/duplicates?table=supplier` and `POST /api/duplicates/<id>/dismiss` return the same shapes as before.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Move the listing and dismissal into the service, switch the views to it, implement the five tools, register, add `SPECS`. Descriptions:
  - `find_duplicates`: "Scan one table for records that look like the same thing (fuzzy match on text columns). Returns the groups found."
  - `list_duplicates`: "Duplicate groups already found, by table and status (open, dismissed, merge_pending, merged)."
  - `get_duplicate`: "The records of one duplicate group side by side, and the tables that reference them."
  - `merge_duplicates`: "Propose merging a group into one surviving record: references move to the survivor and the others are deactivated. This is a data change and goes through approval. Show the user the records first and ask which one to keep."
  - `dismiss_duplicate`: "Mark a group as not duplicates, so a scan does not bring it back."
- [ ] **Step 4:** Run the whole suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(mcp): duplicates tools`.

### Task 5: Schema export

**Files:**
- Modify: `config/siconfig.py`, `settings_app/tunables.py`, `mcp_server/tools.py`, `mcp_server/app.py`, `chat/toolspecs.py`
- Create: `tests/test_mcp_export.py`

**Interfaces:**
- Consumes: `schema.export.open_export(schema, *, data, cap) -> Export` with `.chunks()`, `.filename()`, `.counts`, `.close()`; `dump_model.ExportError` (has `.status`).
- Produces:
  - Tunable `limits.maxInlineExportBytes`: label "Inline export size (bytes)", help "The largest schema export an AI client receives inline. Bigger exports are downloaded in the web app.", int, min 1024, max 5000000, default 200000 (in `siconfig` defaults and `tunables`).
  - `tools.export_schema(user, schema: str = "master", include_data: bool = False) -> {"filename": str, "sql": str, "bytes": int, "truncated": bool, "note": str}`. When truncated, `sql` ends at the last statement end (`";\n"` or `"\\.\n"`) at or before the cap and `note` reads "Cut at the inline limit. Download the full file in the web app: Database, then Download SQL."; otherwise `note` is `""`.
  - `tools.cut_sql(text: bytes, cap: int) -> tuple[bytes, bool]` (pure helper for the cut).

- [ ] **Step 1: Write the failing tests**:
  - Pure: `test_cut_sql`: under the cap returns the input and false; exactly at the cap returns the input and false; one byte over returns a prefix that ends with `;\n` and true; input with no statement end inside the cap returns `b""` and true.
  - With the `master` fixture and two small tables with rows: `test_structure_export` (sql contains `CREATE TABLE` for both, `truncated` false, filename ends `.sql`); `test_data_needs_an_editor` (viewer with `include_data=True` raises `ToolFailure` naming the editor role; editor gets `COPY` blocks); `test_empty_database_exports` (no tables: a header, not an error); `test_truncates_at_the_setting` (set the tunable to 1024 through `settings_app.services`, export, `truncated` true, `len(sql.encode()) <= 1024`, `note` non-empty); `test_unknown_schema` (`ToolFailure` with `list_schemas`); `test_export_is_audited` (an audit entry `schema.export` with `{"schema", "data", "via": "mcp"}`); `test_tunable_is_listed` (`get_settings` includes `limits.maxInlineExportBytes` with default 200000).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Read chunks until the cap plus one byte is reached, then stop and close the export (always close in `finally`). The row cap for data is the existing `limits.maxExportRows`. Register and add `SPECS`. Description: "A database (schema) as Postgres SQL: structure, and data too with include_data=true (editors). Returned inline up to a size limit; when 'truncated' is true tell the user to download the full file in the web app."
- [ ] **Step 4:** Run the whole suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(mcp): export_schema tool with an inline size limit`.

### Task 6: Search reindex, instructions and end-to-end listing

**Files:**
- Modify: `mcp_server/tools.py`, `mcp_server/app.py`, `chat/toolspecs.py`, `tests/test_mcp_http.py`, `README.md`
- Create: `tests/test_mcp_reindex.py`

**Interfaces:**
- Consumes: `search.indexer.reindex(*, rebuild: bool = False) -> {"embedded","unchanged","removed"}`, `search.embedder.SearchError`.
- Produces: `tools.reindex_search(user, rebuild: bool = False) -> dict` (the indexer's result; records audit action `search.reindexed`).

- [ ] **Step 1: Write the failing tests**: `test_reindex_is_for_approvers` (`allowed`); `test_reindex_returns_counts` (monkeypatch `search.embedder.embed` to return fixed-size vectors, as `tests/test_search.py` does; result has the three keys; an audit entry exists); `test_reindex_reports_an_embedding_failure` (make `embed` raise `SearchError("embed service is not reachable")`; the tool raises `ToolFailure` with that text).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement, register, add `SPECS`. Description: "Refresh the schema search index (approvers). Runs by itself after schema changes; use rebuild=true only when search results look wrong."
- [ ] **Step 4:** Update `tests/test_mcp_http.py::test_each_role_sees_only_its_tools` so its expected sets are computed from `tools.TOOL_ROLES` by rank (not hand lists), and add `test_new_tools_work_over_http`: as approver call `list_changes` and `read_rows` through the MCP client and get structured content back.
- [ ] **Step 5:** Rewrite `INSTRUCTIONS` in `mcp_server/app.py` to this text:

```
Singularity manages master-data PostgreSQL databases (each "database" is a Postgres schema; master is the default).
Start with get_rules and list_schemas. Find things with list_tables, describe_table, search_schema, read_rows
(one table, paged) and query (one read-only SELECT).
You never write SQL to change anything: call propose_change with structured operations. The gate checks the
proposal against the team's rules and returns a diff. Show the diff to the user and ask for confirmation before
calling approve_change. Schema changes by editors wait for an approver. Dropping things needs a 'reason'.
Never say a change is applied until its status is 'applied'.
History: list_changes and get_change; approvers also have get_audit_log, revert_change and list_pending.
A stale change is re-checked with recheck_change.
Data quality: find_duplicates, list_duplicates, get_duplicate, then merge_duplicates (a data change) or dismiss_duplicate.
Settings, gate rules and roles: get_settings and list_users, then propose_config_change. These always wait for an approver.
Whole SQL files: submit_script parses a pg_dump-style script into the same gated operations. export_schema returns a
database as SQL.
Every tool states the role it needs. If a tool is missing for the user, say which role has it.
```

- [ ] **Step 6:** In `README.md`, section "Connect an AI client (MCP)", add a table of all tools with one line each and the minimum role, generated by reading `TOOL_ROLES` and the descriptions (write it by hand from them; no generator script).
- [ ] **Step 7:** Run the whole suite. Expected: PASS. Run `python -c "from mcp_server.app import build_app; build_app()"` with the test environment. Expected: no output, exit 0.
- [ ] **Step 8:** Commit: `feat(mcp): reindex tool, server instructions and README for the new tools`. Push the branch and open a pull request against `main` titled `MCP reach: history, audit, revert, rows, duplicates, export, reindex`. The body lists each tool with its role, the service functions that moved out of views, what was tested against a real Postgres, and what was not checked (a real MCP client such as Claude or Gemini connecting to the deployed server).
