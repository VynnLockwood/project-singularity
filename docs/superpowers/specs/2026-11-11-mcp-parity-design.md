# MCP parity: every database task can be done from MCP — design

## Intent (what the owner said, and what I assumed)

Said: "let make us to done everything db task finish on mcp not our web, it's my final big goal." Approved the proposal in chat with its defaults: backups are approver-only, one spec for all five parts.

Success: an AI client that is chat-only (no shell, no files) can do every database task a person can do in the web app. The web app stays for people who prefer it, and for what must not be an AI's to do.

Assumed (correct me): "db task" means working on the master-data databases (schemas, tables, rows, rules that govern them, duplicates, backups, scripts) and not the shell around them (sign-in, tokens, chat history, look and feel).

## What is already on MCP (not touched here)

Read and explore (`list_schemas`, `list_tables`, `describe_table`, `read_rows`, `query`, `search_schema`, `export_schema`), change (`propose_change`, `propose_sql`, `propose_config_change`, `approve_change`, `reject_change`, `revert_change`, `recheck_change`), history (`list_changes`, `get_change`, `get_audit_log`, `list_pending`), duplicates (`find_duplicates`, `list_duplicates`, `get_duplicate`, `merge_duplicates`, `dismiss_duplicate`), scripts (`submit_script`, `validate_script`, `get_script`), big loads (`begin_load`, `append_rows`, `finish_load`, `get_load`, `cancel_load`), settings (`get_rules`, `get_settings`, `list_users`, `reindex_search`).

## The gaps, verified in code

| Web does it | Where | MCP today |
|---|---|---|
| Rule version history and rollback | `settings_app/views.py` `rule_versions`, `rule_rollback`; `services.rollback_rule` | `propose_config_change` can edit a rule but cannot see its history or roll it back |
| Reopen a dismissed duplicate group | `duplicates/views.py` `reopen` | only `dismiss_duplicate` |
| Make a backup | `schema/backup.py` (used inside approvals) | no tool |
| List backups | files in `BACKUP_DIR` | no tool |
| Look at what a column holds | grid column stats | `query` only, written by hand each time |
| Pick a reference value (FK dropdown) | `schema/rows.py` `options` | no tool |
| Table CSV download | `/api/schema/tables/<t>/export.csv` | `read_rows` pages the same data; a chat client cannot download a file anyway |

## Design

### 1. Rules: history and rollback
- `get_rule_history(rule)` — read, any role. Returns versions newest first: `{version, action, snapshot, changed_by, created_at}`. Same data as the web endpoint.
- New config operation `rollback_rule` in `approvals/config_ops.py`: `{"op":"rollback_rule","name":<rule>,"version":<int>}`. It goes through `propose_config_change`, so it waits for an approver like every other configuration change, and an approver's own proposal self-approves as today. Apply calls `services.rollback_rule`. Reverse is a `rollback_rule` to the version current just before applying (history only moves forward, so the reverse is also a new version). Unknown rule or version is a plain error from the gate.
- `get_rules` and `get_settings` descriptions mention it.

### 2. Duplicates: reopen
- `reopen_duplicate(group_id)` — editor. Only a `dismissed` group can be reopened; `merged` and `merge_pending` return a clear error. New `service.reopen(g, user)` writes the audit record `duplicates.reopened`; the web view is changed to call it (so the web app also gets the audit record and the status check).

### 3. Backups (approver only)
- `create_backup(schema="master", label="manual")` — runs `backup_schema`, returns `{file, schema, bytes, created_at}`; writes `backup.created` to the audit log. Uses the configured retention. Errors (`BackupError`) come back as a tool error with the reason.
- `list_backups(schema=None)` — newest first: `{file, schema, label, bytes, created_at}` from the files in `BACKUP_DIR` that match `DUMP_RE`.
- **Not included: restore.** Restoring replaces data outside the gate; it stays a Postgres-admin action, and `list_backups` plus the file name is what an admin needs. Stated here so the goal's boundary is explicit.
- `create_backup` blocks up to the existing 600 s `pg_dump` timeout; the tool description says so.

### 4. Reading data
- `profile_column(table, column, schema="master")` — read, any role. One read-only query set on the existing read-only connection: `total`, `null_count`, `distinct_count`, `min` and `max` (only for types that order: numbers, dates, text), and `top_values` (up to 10 of `{value, count}`, values cut at 80 characters). Counts only active rows when the table has an `is_active` role column, same as the grid and `options`. Unknown table or column is a plain error. New function `schema/rows.py: profile`.
- `lookup_options(table, q="", limit=100, schema="master")` — read, any role. Calls `rows.options`; returns `{label_column, truncated, options:[{id,label}]}`. Lets an AI turn "Bangkok" into the id to put in a foreign-key column.

### 5. Parity test (keeps the goal true)
`tests/test_mcp_parity.py` lists every `api/` route from `config.urls` and requires each to appear in exactly one of two tables kept in the test file:
- `COVERED = {route: [tool names]}` — every tool name must exist in the MCP registry;
- `WEB_ONLY = {route: reason}` — the reason is written down: sign-in and tokens, chat conversations and models (the web's own assistant), health, queue badge counts, CSV download.
A new web route with no entry fails the suite, so a task added to the web app later cannot silently stay web-only.

### Shared plumbing
New tools follow the existing pattern in `mcp_server/tools.py` (function, `DESCRIPTIONS` entry, `allowed` role gate) and `mcp_server/app.py` (`@mcp.tool` wrapper using `run`). Role rules: read tools any role, `reopen_duplicate` editor, backups approver; a tool above the caller's role is hidden and refused, as now. Whether the web chat also gets a new tool follows the existing `chat/toolspecs.py` single-source mechanism; no chat-only behavior is added.

## Out of scope
Restore from a backup; MCP token and OAuth management (an AI should not mint its own credentials); chat conversations; any web UI change except the `reopen` view using the service.

## Testing
Each tool: a test per role (allowed, hidden, refused), the happy path on the test master database, and its error cases (unknown rule/version/table/column/group, wrong group status, no `pg_dump`). `rollback_rule`: apply, then revert through `revert_change`, and the gate refuses an editor's proposal for approval. Parity test as above. Existing suites stay green.

## Review focus (for the final review)
1. `rollback_rule` reverse really restores the earlier state, and history stays strictly increasing.
2. `profile_column` and `lookup_options` cannot be made to run arbitrary SQL through `table` or `column` (identifier quoting), and cannot read a table outside the schema argument.
3. `create_backup` cannot write outside `BACKUP_DIR` (label and schema checks) and is refused below approver.
4. `reopen_duplicate` refuses merged and merge-pending groups and a group of a table the caller cannot read.
5. The parity test fails when a route is added with no entry, and passes now.

## Rulings I made while designing
- Rollback goes through `propose_config_change` (gated, approver-applied) instead of a direct approver-only tool, so MCP "stays king" through the same gate as every other setting change.
- `reopen` gains a status check and an audit record in the web view too, because two code paths with different rules is how they drift apart. Cost if wrong: the web app can no longer reopen a merged group (it should not).
- Restore is excluded on purpose (see above).
