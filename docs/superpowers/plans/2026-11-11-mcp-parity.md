# MCP Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the verified gaps so a chat-only AI client can do every database task a person can do in the web app (rule history/rollback, reopen duplicate, backups, column profile, reference options), and keep it that way with a parity test.

**Architecture:** Each new tool follows the existing four-place pattern: function + `DESCRIPTIONS` + `TOOL_ROLES` + `IMPL` in `mcp_server/tools.py`, an `@mcp.tool` wrapper in `mcp_server/app.py`, and a `SPECS` entry in `chat/toolspecs.py`. Rule rollback is a new `rollback_rule` config op so it goes through the existing gate (`propose_config_change`, approver applies, reversible). A route-by-route parity test fails when a new `api/` route has no MCP tool or stated reason.

**Tech Stack:** Django 5, psycopg `sql.Identifier` composition, FastMCP, pytest (`django_db(transaction=True)`, `master` fixture).

**Spec:** `docs/superpowers/specs/2026-11-11-mcp-parity-design.md`

## Global Constraints

- Roles: read tools (`get_rule_history`, `profile_column`, `lookup_options`, `list_backups` excepted) are `viewer`; `reopen_duplicate` is `editor`; `create_backup` and `list_backups` are `approver`.
- Restore, MCP token/OAuth management and chat conversations are out of scope. The only web change allowed is the duplicates `reopen` view calling the new service.
- No tool may build SQL from table/column text: identifiers go through `psycopg.sql.Identifier` after validation by `rows._describe`; values are parameters.
- `create_backup` writes only through `schema.backup.backup_schema` (fixed `BACKUP_DIR`, validated schema and label); retention comes from `cfg["backups"]["retentionDays"]`.
- `create_backup` description must say it blocks up to 600 s (the pg_dump timeout).
- `top_values` values cut at 80 characters, at most 10; `lookup_options` limit capped by `rows.MAX_PAGE`.
- Errors surface as `ToolFailure` with a hint to the listing tool, like the existing tools.
- Existing suites stay green. Never run two pytest runs at once (shared test DB).

## Review Focus

1. `rollback_rule` reverse restores the earlier state and `RuleVersion` history strictly increases (version after apply+revert is current+2).
2. `profile_column` / `lookup_options` cannot be pointed at arbitrary SQL through `table`/`column` (e.g. `"x; drop table y"`, a quoted identifier, a column of another table) and cannot read outside the `schema` argument (unknown or non-registered schema → error).
3. `create_backup` is refused below approver, a hostile `label` (`../x`) is rejected, and files land only inside `BACKUP_DIR`; `list_backups` ignores files not matching `DUMP_RE`.
4. `reopen_duplicate` refuses merged and merge-pending groups, and a group whose table the caller cannot read.
5. Parity test fails when a route has no entry (prove it with a fake route) and passes now.

---

### Task 1: Rule history and rollback

**Files:**
- Modify: `backend/approvals/config_ops.py` (`CONFIG_OPS`, apply loop, new `_rollback_rule`)
- Modify: `backend/mcp_server/tools.py` (`get_rule_history`, `DESCRIPTIONS`, `TOOL_ROLES`, `IMPL`, `propose_config_change` description mentions `rollback_rule`)
- Modify: `backend/mcp_server/app.py`, `backend/chat/toolspecs.py`
- Test: `backend/tests/test_mcp_rules_history.py`

**Interfaces:**
- Consumes: `settings_app.services.rollback_rule(actor, rule, version) -> Rule` (raises `RuleError.errors`), `RuleVersion` (`version, action, snapshot, changed_by, created_at`), `Rule.snapshot()`, `Rule.version`.
- Produces: op `{"op":"rollback_rule","name":str,"version":int}` accepted by `propose_config_change`; tool `get_rule_history(user, rule: str) -> {"rule": str, "current_version": int, "versions": [{"version","action","snapshot","changed_by","created_at"}]}` newest first; role `viewer`.

- [ ] **Step 1: Write failing tests** in `test_mcp_rules_history.py` (users fixture via `make_users`, approver creates a rule with `applied`-style `propose_config_change` + `approve_change`, then updates it once):
  - `test_history_lists_versions_newest_first`: versions `[2, 1]`, `current_version == 2`, `changed_by` is the email.
  - `test_history_unknown_rule_is_a_tool_failure`: `ToolFailure`, message points at `get_rules`.
  - `test_history_visible_to_viewer`.
  - `test_rollback_applies_then_reverts` (Review Focus 1): rollback to 1 via `propose_config_change` + `approve_change` → rule params equal version-1 snapshot, `Rule.version == 3`, history has 3 rows; `revert_change` → params equal version-2 snapshot again, `Rule.version == 4`, 4 history rows (strictly increasing).
  - `test_editor_rollback_waits_for_approval`: editor's proposal returns `pending`, rule unchanged.
  - `test_rollback_unknown_rule_or_version_is_a_gate_error`: both produce errors in the proposal result, nothing created.
- [ ] **Step 2: Run** `pytest tests/test_mcp_rules_history.py -q`. Expected: FAIL (tool/op missing).
- [ ] **Step 3: Implement** `_rollback_rule(res, actor, op)` in `config_ops.py`, same shape as `_update_rule`: look up rule by name (error `rule <name> does not exist` if missing), remember `before_version = rule.version`, call `rule_services.rollback_rule`, on `RuleError` add `rule <name>: <e>` to `res.errors`, diff line `~ rule <name>: rolled back to version <n>`, reverse `{"op":"rollback_rule","name":name,"version":before_version}`. Add to `CONFIG_OPS`. Check `propose_config_change` validates op names against `CONFIG_OPS` and that op validation requires `name` string and `version` int (reject bool).
- [ ] **Step 4: Implement** `get_rule_history(user, rule: str)` in `tools.py` reading `Rule.objects.filter(name=rule)`; wire `TOOL_ROLES["get_rule_history"]="viewer"`, `IMPL`, `DESCRIPTIONS` (say: pair with `propose_config_change` `rollback_rule` to undo), app.py wrapper `async def get_rule_history(rule: str)`, instruction line in the settings/rules family, SPECS entry `{"rule": {"type": "string"}}`, required `["rule"]`.
- [ ] **Step 5: Run** `pytest tests/test_mcp_rules_history.py tests/test_mcp_tools_registry.py -q`. Expected: PASS (registry test updates are in Step 6 of Task 5 if the pinned role list needs the new names; fix here if it fails).
- [ ] **Step 6: Commit** `feat(mcp): rule history tool and rollback_rule config op`.

### Task 2: Reopen a dismissed duplicate group

**Files:**
- Modify: `backend/duplicates/service.py` (`reopen(g, user)`), `backend/duplicates/views.py` (`reopen` view)
- Modify: `backend/mcp_server/tools.py`, `app.py`, `chat/toolspecs.py`
- Test: `backend/tests/test_mcp_duplicates.py` (extend), `backend/tests/test_duplicates_api.py` or the existing duplicates view test file (extend for the web view)

**Interfaces:**
- Consumes: `service.get_group(group_id)`, `DuplicateGroup.Status` (`OPEN`, `DISMISSED`, merged/merge-pending values), `service.summary(g)`, `service.DuplicateError(message, status)`, `record(user, action, ...)`, the `_dup` tool helper, and whatever read-access check `dismiss` already performs for the group's table.
- Produces: `service.reopen(g, user) -> dict` (summary of the reopened group); tool `reopen_duplicate(user, group_id: int) -> dict`, role `editor`.

- [ ] **Step 1: Write failing tests** (reuse the `users` fixture; build a group with `find_duplicates`, dismiss it):
  - `test_reopen_dismissed_group`: status back to open, audit row `duplicates.reopened` exists with the group id.
  - `test_reopen_refuses_merged_group` and `test_reopen_refuses_open_group` (Review Focus 4): `ToolFailure` with a clear message, status unchanged, no audit row.
  - `test_reopen_unknown_group_hints_list_duplicates`.
  - `test_reopen_hidden_from_viewer` via `tools.allowed`.
  - `test_reopen_unreadable_table_refused`: follow whatever mechanism `dismiss`/`get_duplicate` uses to refuse groups whose table the caller cannot read; mirror its test. If the existing code has no such check, add the same check `get_duplicate` performs to `reopen` and record a Ruling.
  - Web: `test_web_reopen_checks_status_and_audits`: POST `/api/duplicates/<id>/reopen` on a merged group → 409/400 (use the status `DuplicateError` carries), on a dismissed group → 200 and an audit row.
- [ ] **Step 2: Run** the new tests. Expected: FAIL.
- [ ] **Step 3: Implement** `service.reopen`: raise `DuplicateError` unless status is `DISMISSED` (message names the current status), set `OPEN`, save, `record(user, "duplicates.reopened", ...)` with the same target/details shape as `dismiss`, return `summary(g)`. Change the web `reopen` view to call it and map `DuplicateError` the way sibling views do. Add the tool via `_dup(lambda d: d.reopen(d.get_group(group_id), user))`, role `editor`, description, app.py wrapper, SPECS entry like `dismiss_duplicate`.
- [ ] **Step 4: Run** `pytest tests/test_mcp_duplicates.py <duplicates web test file> -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(mcp): reopen_duplicate; web reopen now checks status and audits`.

### Task 3: Backups, approver only

**Files:**
- Modify: `backend/schema/backup.py` (`list_backups(schema=None) -> list[dict]`, parsing regex)
- Modify: `backend/mcp_server/tools.py`, `app.py`, `chat/toolspecs.py`
- Test: `backend/tests/test_mcp_backups.py`

**Interfaces:**
- Consumes: `backup_schema(schema, label, retention_days) -> Path`, `BackupError`, `DUMP_RE`, `SCHEMA_RE`, `settings.BACKUP_DIR`, `introspect.UnknownSchema`, the retention lookup used in `approvals/service.py` (`cfg["backups"]["retentionDays"]`) and `record`.
- Produces: `backup.list_backups(schema: str | None = None) -> list[dict]` newest first, items `{file, schema, label, bytes, created_at}` (ISO UTC from the file-name stamp); tools `create_backup(user, schema: str = "master", label: str = "manual") -> {file, schema, bytes, created_at}` and `list_backups(user, schema: str | None = None) -> {"backups": [...]}`; both role `approver`; audit action `backup.created`.

- [ ] **Step 1: Write failing tests** with `BACKUP_DIR` pointed at `tmp_path` via `settings` fixture:
  - `test_create_backup_writes_a_dump_and_audits`: file exists in `tmp_path`, result has bytes > 0, audit `backup.created`. Skip with a clear reason if `pg_dump` is not installed (match how existing backup tests behave).
  - `test_create_backup_uses_configured_retention`: monkeypatch `backup_schema` to capture `retention_days`, set `backups.retentionDays` to 3 via the settings mechanism the other tests use; assert 3.
  - `test_create_backup_refused_below_approver` (Review Focus 3): editor and viewer get the same "needs approver" refusal other tools give; `tools.allowed` false.
  - `test_hostile_label_and_schema_rejected`: `label="../x"`, `schema="master; drop"`, unknown schema → `ToolFailure`, nothing written outside `tmp_path`.
  - `test_backup_error_becomes_tool_failure`: monkeypatch `backup_schema` to raise `BackupError("pg_dump is not installed")`; message passes through.
  - `test_list_backups_newest_first_and_filtered`: drop files named `master-20260101T000000Z-manual.dump`, `master-20260102T000000Z-change-5.dump`, `other-20260103T000000Z-x.dump`, plus `notes.txt` and `bad.dump` → only the three valid ones, newest first; `schema="master"` filter returns two; `label` parsed (`change-5`).
- [ ] **Step 2: Run** `pytest tests/test_mcp_backups.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `list_backups` in `backup.py` using a named-group regex `^(?P<schema>[a-z][a-z0-9_]*)-(?P<stamp>\d{8}T\d{6}Z)-(?P<label>.+)\.dump$` (consistent with `DUMP_RE`), skipping non-matches, missing folder → `[]`. Tools: validate schema is a registered schema through the same check `export_schema` uses before calling `backup_schema`; map `BackupError` to `ToolFailure`; `DESCRIPTIONS["create_backup"]` states "blocks until pg_dump finishes (up to 10 minutes)" and "there is no restore tool; a person restores in the web app or with pg_restore". Wrapper + SPECS entries (`schema`, `label` optional).
- [ ] **Step 4: Run** `pytest tests/test_mcp_backups.py -q`. Expected: PASS (or skip only on missing `pg_dump`).
- [ ] **Step 5: Commit** `feat(mcp): create_backup and list_backups for approvers`.

### Task 4: Column profile and reference options

**Files:**
- Modify: `backend/schema/rows.py` (`profile`)
- Modify: `backend/mcp_server/tools.py`, `app.py`, `chat/toolspecs.py`
- Test: `backend/tests/test_mcp_read_tools.py`

**Interfaces:**
- Consumes: `rows._describe`, `rows._run(query, params)`, `rows.options(table, *, q, limit, schema)`, `rows.RowsError(message, status)`, `rows.jsonable`, `roles.conventions()/roles_of(d, conv)` (`is_active` role column), the error mapping `read_rows` uses for `RowsError`/`UnknownSchema`.
- Produces: `rows.profile(table: str, column: str, *, schema: str = "master") -> dict` with `total, null_count, distinct_count, min, max, top_values[{value,count}]` (`min`/`max` only for orderable types, otherwise `None`); tools `profile_column(user, table, column, schema="master")` and `lookup_options(user, table, q="", limit=100, schema="master") -> {label_column, truncated, options}` (note snake_case key `label_column`, mapped from `labelColumn`); both role `viewer`.

- [ ] **Step 1: Write failing tests** (approver builds `supplier` with `name text`, `rating int`, `is_active boolean`, ~15 rows with nulls, repeats, one value 200 chars long, some inactive rows):
  - `test_profile_counts`: `total`, `null_count`, `distinct_count` as hand-computed; counts only active rows because `is_active` exists.
  - `test_profile_top_values_capped_and_cut`: at most 10 entries, ordered by count desc, long value cut to 80 chars.
  - `test_profile_min_max_for_orderable_only`: `rating` has min/max; a `jsonb` or boolean-free text column behaves per type (text is orderable; `jsonb` gives `None`).
  - `test_profile_cannot_take_sql_through_names` (Review Focus 2): `column="name; drop table supplier"`, `column='"name"'`, `table="supplier; select 1"` → `ToolFailure` hinting `describe_table`; table still exists afterwards.
  - `test_profile_unknown_column_and_table`, `test_profile_unknown_schema` (error, nothing read).
  - `test_profile_and_options_visible_to_viewer`.
  - `test_lookup_options_matches_web_options`: tool output equals `rows.options(...)` content (ids, labels, truncated), `q` filters, `limit` clamps to `MAX_PAGE`.
  - `test_lookup_options_table_without_primary_key_is_a_tool_failure`.
- [ ] **Step 2: Run** `pytest tests/test_mcp_read_tools.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `rows.profile`: `d = _describe(table, schema)`, reject a `column` not in `d["columns"]` names with `RowsError("column not found", 404)` before any SQL; build one aggregate query with `sql.Identifier(schema, table)` / `sql.Identifier(column)` (`count(*)`, `count(col)`, `count(DISTINCT col)`, and `min`/`max` over `col` only when the data type is in an orderable set — numeric, text, date/time, boolean excluded for min/max of `json`/`jsonb`/`bytea`/arrays), then a second query for top values (`GROUP BY col ORDER BY count(*) DESC, col LIMIT 10` on `col::text`, cut with `left(..., 80)`); apply `WHERE <is_active> IS DISTINCT FROM false` when the role column exists; pass results through `jsonable`. Tools reuse the `read_rows` error mapping; `lookup_options` renames `labelColumn`. Descriptions say when to use each (profile before designing constraints; options to find ids before writing FK values).
- [ ] **Step 4: Run** `pytest tests/test_mcp_read_tools.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(mcp): profile_column and lookup_options`.

### Task 5: Parity test, registry pins and docs

**Files:**
- Create: `backend/tests/test_mcp_parity.py`
- Modify: `backend/tests/test_mcp_tools_registry.py` (pin roles of the new tools; keep `NOT_IN_CHAT` unchanged — all new tools are in chat)
- Modify: `backend/mcp_server/app.py` (instruction block lines for the new families, if not already added in Tasks 1-4), `README` section on MCP tools if one lists them

**Interfaces:**
- Consumes: every route from `config.urls` (`get_resolver().url_patterns`, walk nested includes, keep patterns whose route string starts with `api/`), `tools.TOOL_ROLES`.
- Produces: `COVERED: dict[str, list[str]]` and `WEB_ONLY: dict[str, str]` in the test module; test fails with the offending route listed.

- [ ] **Step 1: Write failing tests** in `test_mcp_parity.py`:
  - `test_every_api_route_is_covered_or_web_only`: each route is in exactly one of `COVERED` / `WEB_ONLY`; failure message lists unmapped routes and routes in both.
  - `test_covered_tools_exist`: every tool named in `COVERED` is in `tools.TOOL_ROLES`.
  - `test_no_stale_entries`: every key of `COVERED`/`WEB_ONLY` is a real route.
  - `test_parity_test_catches_a_new_route` (Review Focus 5): call the same checking helper with the route set plus `"api/brand-new"` and assert it reports it.
  Fill `COVERED` from the real route list: changes → `list_changes,get_change,propose_change,approve_change,reject_change,revert_change,recheck_change`; queue → `list_pending`; settings → `get_settings,propose_config_change`; rules → `get_rules,propose_config_change`; rule versions/rollback → `get_rule_history,propose_config_change`; schema routes → `list_schemas,list_tables,describe_table,export_schema,read_rows,lookup_options,profile_column`; query → `query`; duplicates routes → the duplicates tools including `reopen_duplicate`; search → `search_schema,reindex_search`; audit → `get_audit_log`; users → `list_users`. `WEB_ONLY` with reasons: sign-in and tokens, chat conversations/models, health, queue badge counts, CSV download (covered by `read_rows` paging — list it under COVERED instead if the route is exactly the CSV export). Read `config/urls.py` in full to enumerate; a route you cannot place is a Ruling, never a silent entry.
- [ ] **Step 2: Run** `pytest tests/test_mcp_parity.py -q`. Expected: FAIL until the map is complete, then PASS with Tasks 1-4 merged. If it passes immediately, temporarily delete one entry to see it fail, then restore.
- [ ] **Step 3: Update** registry role pin test with `get_rule_history/viewer, reopen_duplicate/editor, create_backup/approver, list_backups/approver, profile_column/viewer, lookup_options/viewer`; confirm app.py instruction block and README mention each new tool family (one line each).
- [ ] **Step 4: Run the full suite** in the background (`pytest -q`, ~7 min, poll with sleeps ≤115 s). Expected: all pass, no regressions.
- [ ] **Step 5: Commit** `test(mcp): route-by-route parity test and registry pins`.

---

## Self-review

- Spec coverage: parts 1-5 map to Tasks 1-5; shared plumbing (TOOL_ROLES, DESCRIPTIONS, wrapper, SPECS) is in each tool task; out-of-scope items stay out; all five Review Focus items have a named test in the owning task.
- Types: `get_rule_history` → `{rule,current_version,versions}`; `lookup_options` returns `label_column` (tool) from `labelColumn` (rows); backup items `{file,schema,label,bytes,created_at}` used identically in Task 3 tests and tool.
- Proportion: bodies are described by signature and assertions; only the profile SQL shape and the file-name regex are spelled out because the signature does not decide them.
