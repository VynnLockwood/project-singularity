# MCP full coverage: every app feature, more gated operations, and gated raw SQL

Date: 2026-11-10. Status: design approved by the product owner in chat; this file is for written review.
Base: `origin/main` at c334eaa (PRs 1 to 21 merged). Independent of PR 22 (UI shell), except section 4.7.

## 0. Summary

The owner wants an AI client to be able to do everything about the database through MCP tools. Today the MCP server has 17 tools and the gate has 22 operations. Three gaps, three plans, one pull request each, in this order:

1. **Plan A: reach.** Tools for features the web app already has but MCP does not. No new power.
2. **Plan B: operations.** New gated operations for common database work the gate cannot express yet.
3. **Plan C: raw SQL.** One tool, `propose_sql`, for anything else. It skips the rules gate, so it gets the strictest approval path.

### Decisions taken (from the owner)

| Question | Decision |
|---|---|
| How far does "everything" go? | Option C: app features, new gated operations, and a raw SQL escape hatch. |
| Who may propose raw SQL? | Editors and approvers. |
| Who may approve raw SQL? | Approvers only, and only in the web app. No AI client can approve it. |
| Functions, triggers, grants | No dedicated tools; they go through `propose_sql`. |
| Extensions | Stay impossible: the database role that applies changes is not a superuser. |

### Assumptions (change me if wrong)

- Every change still becomes a `Change` row, appears in the queue, is audited, and is applied by the existing approve step. Nothing writes to the database outside that path.
- Raw SQL runs as `singularity_master_rw`, the role the gate already uses. That role has no rights on the `singularity` schema (users, approvals, audit, settings) or on `public`, so Postgres itself keeps raw SQL inside the managed databases.
- The web chat assistant gets the same new tools as an MCP client of the same role (`chat/toolspecs.py`), with the same existing exception: it has no approve tool.
- Tool results stay small enough for an AI client's context: every list is capped and says when it was cut.
- Out of scope: other Postgres servers, scheduled jobs, a restore-from-backup tool, row-level history.

## 1. What stays the same

- `mcp_server/tools.py` keeps its shape: plain functions `(user, ...) -> dict`, `TOOL_ROLES` for the minimum role, `IMPL` as the registry, `ToolFailure` for errors the AI can act on. `mcp_server/app.py` stays a thin SDK adapter.
- Every tool result that involves a change carries the `next` hint (`_next_step`), so the AI knows whether to ask the user, wait for an approver, or fix and retry.
- Role rules come from the shared services, never from the tool layer.

## 2. Plan A: tools for features the app already has

Each tool calls an existing service function. Where the REST view holds logic the service does not, that logic moves into the service so the tool and the view share it.

| Tool | Min role | Calls | Result |
|---|---|---|---|
| `list_changes` | viewer | `Change` query, same filters as `GET /api/changes` | `{changes: [...], truncated}`; filters `status`, `kind`, `schema`, `mine`, `limit` (default 20, max 100); summaries only (`serialize(detail=False)`). |
| `get_audit_log` | approver | `AuditLog` query | `{entries: [...], truncated}`; filters `action`, `actor`, `limit` (default 50, max 200). |
| `revert_change` | approver | `service.revert` | The revert change, with `next`. |
| `recheck_change` | editor | `service.recheck` | The change, with `next`. |
| `read_rows` | viewer | `rows.read_rows` | One page: `columns`, `rows`, `total`, `offset`, `limit`. Arguments `table`, `schema`, `offset`, `limit` (max 200), `sort`, `direction`, `filters`, `include_inactive`. |
| `find_duplicates` | editor | `duplicates.service.scan` | Scan summary: groups found, capped list of group summaries. |
| `list_duplicates` | viewer | `DuplicateGroup` query | Group summaries for one table, `status` filter. |
| `get_duplicate` | viewer | `duplicates.service.detail` | The records side by side. |
| `merge_duplicates` | editor | `duplicates.service.propose_merge` | The proposed data change, with `next`. |
| `dismiss_duplicate` | editor | existing dismiss logic | The group summary. |
| `export_schema` | viewer (structure), editor (with data) | `schema.export.open_export` | `{filename, sql, truncated}`. Capped at `limits.maxInlineExportBytes` (new tunable, default 200000). Over the cap: the SQL is cut at a statement boundary, `truncated` is true, and the result says to download the full file in the web app. Audited as `schema.export` like the REST endpoint. |
| `reindex_search` | approver | `search.indexer.reindex` | `{embedded, unchanged, removed}`. |

`get_audit_log` is approver-only because audit details can hold other people's prompts and emails. The REST endpoint is open to every signed-in user today; this spec does not change that, and flags it for the backend health piece.

## 3. Plan B: new gated operations

Each new operation is a full citizen of the gate: validated in `gate/operations.py`, modelled in `gate/state.py`, checked by the rules, turned into SQL by `gate/sqlgen.py`, executed and reversed by `schema/executor.py`, shown as a diff line, and accepted by the script intake mapper where a matching SQL statement exists.

| Operation | Kind | Fields | Reverse | Notes |
|---|---|---|---|---|
| `set_column_default` | schema | `table`, `column`, `default` | previous default (set or drop) | `default` is a scalar literal or one of the existing `DEFAULT_FUNCS`. |
| `drop_column_default` | schema | `table`, `column` | `set_column_default` with the old value | |
| `set_not_null` | schema | `table`, `column` | `drop_not_null` | The dry run fails with the count of NULL rows if any exist. |
| `drop_not_null` | schema | `table`, `column` | `set_not_null` | Refused on primary key columns. |
| `create_view` | schema | `name`, `query`, `comment?` | `drop_view` | `query` must parse (pglast) as exactly one `SELECT` with no `INTO`, no locking clause and no data-modifying CTE. Naming rules apply to the view name. |
| `drop_view` | schema | `name`, `reason` | none (destructive) | |
| `create_enum` | schema | `name`, `values` | `drop` only while unused; else none | Values are non-empty strings, unique, at most 100. |
| `add_enum_value` | schema | `name`, `value` | none (Postgres cannot remove an enum value) | |
| `delete_rows` | data | `table`, `where`, `reason` | `insert_rows` with the deleted rows, when the count is within `maxRowsPerDataOp`; else none | A real `DELETE`. Same `where` grammar as `update_rows`. An empty `where` is refused. Counted in `DESTRUCTIVE_OPS`. Foreign keys that restrict make the dry run fail with the constraint named. |
| `upsert_rows` | data | `table`, `key`, `columns`, `rows` | previous values of updated rows plus `delete_rows` of inserted ones, within the row cap; else none | `key` must match a unique constraint or the primary key. |
| `move_table` | database | `table`, `from_schema`, `to_schema` | `move_table` back | One per change, like the other database-level operations. Refused when a table of that name exists in the target or when views depend on it. |

Enum types become usable as column types: `TYPE_RE` accepts the name of an enum that exists in the target schema.

Rule coverage: naming rules apply to views and enum types. `delete_rows` gets a new built-in rule `hardDeleteNeedsReason` (block, on by default) plus a warning that names the row count. Rules are rows in the database as today; the migration that adds the new rule is additive.

`get_rules` and the `propose_change` tool description list the new operations with one example each, because the AI learns the operation grammar from there.

## 4. Plan C: raw SQL through `propose_sql`

### 4.1 Tool

`propose_sql(sql: str, reason: str, schema: str = "master")`, minimum role editor.

- `reason` is required and at least 10 characters. It is shown to the approver.
- `schema` picks the database; `search_path` is set to it for the run.
- Result: the change, with `next`. For kind `sql`, `next` always says the change waits for an approver in the web app and that the AI cannot approve it, including when the caller is an approver.

### 4.2 The change

- New kind `sql` (`Change.kind` is 8 characters wide; no migration needed for the column, one for the documented choices).
- `operations` holds one entry: `{"op": "raw_sql", "sql": "...", "reason": "..."}`. `generated_sql` holds the statements as split by the parser. `diff` holds one line per statement: its command tag and the rows it touched in the dry run (for example `ALTER TABLE district`, `UPDATE 928 rows`).
- `check_result.violations` always holds one warning: "Rules were not checked: this is raw SQL." Further warnings are added for statements that drop or truncate, and for any statement whose target is not schema-qualified to the chosen database.
- `reverse_operations` is `None`: the card says it cannot be undone automatically and names the backup file taken at apply time.

### 4.3 Checks at propose time

In order; the first failure rejects the proposal with a message the AI can act on.

1. Size: at most `limits.maxSqlBytes` (new tunable, default 100000) and `limits.maxSqlStatements` (new tunable, default 50) statements.
2. Parse with pglast. A syntax error is returned with its position.
3. Refuse by statement type: transaction control (`BEGIN`, `COMMIT`, `ROLLBACK`, `SAVEPOINT`, `PREPARE TRANSACTION`), session and role changes (`SET ROLE`, `SET SESSION AUTHORIZATION`, `RESET ROLE`), `COPY` to or from a file or program, `DO` and `CREATE FUNCTION` in any language other than `sql` or `plpgsql` (those two are allowed, so functions and triggers work), `VACUUM`, `CREATE DATABASE`, `DROP DATABASE`, `ALTER SYSTEM`, `LISTEN`, `NOTIFY`, `LOAD`, and `CREATE`/`DROP SCHEMA` (those have gated operations that keep the managed-schema registry right).
4. Dry run: execute every statement as `singularity_master_rw` inside a transaction with the executor's existing lock and statement timeouts, record command tags and row counts, then roll back. A database error rejects the proposal with the primary message and the statement number.

The refusal list is defence in depth. The real fence is the database role: it cannot read or write the `singularity` schema, has no superuser rights, and owns only the managed schemas.

### 4.4 Approval

- `service.can_decide` returns true for kind `sql` only for approvers. Editors never self-approve raw SQL.
- `tools.approve_change` raises `ToolFailure` for kind `sql`: "Raw SQL is approved by a person in the web app." The REST endpoint used by the web app accepts it. The web chat assistant has no approve tool already.
- `reject_change` works over MCP for kind `sql` (withdrawing is safe).

### 4.5 Apply

On approve, in this order: take a backup of the target schema (`backup.backup_schema`, as for schema changes); open one transaction as `singularity_master_rw` with the timeouts; run the statements; commit. Any error rolls everything back and the change becomes `failed` with the message. On success: refresh the managed-schema counts, schedule a search reindex, record `change.applied` with the full SQL and the backup path, and send the live event so open schema views reload.

The staleness check is by schema fingerprint as for other kinds: if the target schema changed since the dry run, the change becomes `stale` and `recheck_change` runs the dry run again.

### 4.6 Settings

`limits.maxSqlBytes`, `limits.maxSqlStatements` and a switch `sql.enabled` (default on) are tunables: visible in Settings, changeable through `propose_config_change`, audited. With `sql.enabled` off, `propose_sql` is hidden from every client and the REST path refuses kind `sql`.

### 4.7 Web app

The change card shows kind `sql` as "Raw SQL" with the reason, the full SQL always expanded, the dry-run lines, and a standing note that the rules were not checked and there is no automatic revert. This is a small frontend change to `ChangeCard`; it lands after PR 22 merges to avoid a conflict, as the last task of Plan C. Until then the card falls back to its default label and still shows the SQL and warnings, so approval works.

## 5. Server instructions and descriptions

The MCP server's instruction text and each tool description are rewritten to match: start with `get_rules` and `list_schemas`; prefer `propose_change` operations; use `propose_sql` only when no operation fits, and say why in `reason`; never claim a pending change is applied. Descriptions state the minimum role, so a client that cannot see a tool can tell the user who can.

## 6. Error handling

- Every service error reaches the AI as a `ToolFailure` with what went wrong and what to call next. No stack traces, no raw driver messages beyond the primary message.
- Unknown schema, table, change, group or script: a message that names the tool that lists valid values.
- Caps are never silent: a cut result says so and says how to get the rest.

## 7. Testing

- Plan A: one test module per tool group, calling `tools.IMPL` directly as viewer, editor and approver: role refusal, happy path, caps, unknown ids. One MCP end-to-end test confirms the new tools are listed per role (`test_mcp_http.py` pattern).
- Plan B: per operation, the existing layers' test patterns: parse and validate (`test_gate_new_ops.py`), SQL generation, rule checks, execute and reverse against a real Postgres, script-intake mapping where it applies. Round trip: apply then revert leaves the schema dump unchanged.
- Plan C: parser refusals (each refused statement type), size caps, dry-run error reporting with statement number, role fencing against a real Postgres with the real roles (a statement touching `singularity.accounts_user` fails with permission denied), approve path (editor refused, approver over MCP refused, approver over REST applies), atomic rollback on a failing second statement, backup taken before apply, stale and recheck, `sql.enabled` off.
- The whole backend suite stays green. CI already runs it with a real Postgres and the real roles.

## 8. Order and size

| Plan | Pull request | Depends on |
|---|---|---|
| A | `mcp: tools for history, audit, revert, rows, duplicates, export, reindex` | nothing |
| B | `gate: defaults, not-null, views, enums, delete, upsert, move table` | nothing (touches different files from A except `tools.py` descriptions) |
| C | `mcp: gated raw SQL (propose_sql)` | A (shares `tools.py` and descriptions); its last task waits for PR 22 |

Each plan gets its own implementation plan document before work starts.
