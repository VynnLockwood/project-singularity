# Per-database access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-user, per-database (Postgres schema) access: a grant `(user, database) → viewer|editor|approver` capped by the global role, enforced everywhere through one function, managed by admins on the website and over MCP.

**Architecture:** A `SchemaGrant` model and `accounts/access.py` (`effective_role`, `can`, `require`, `readable_schemas`, `affected_schemas`) are the only place that decides. Every entry point (web views, MCP tools, which chat also uses, search, duplicates, scripts/loads/imports, iquery, audit, approvals, live events) calls it. A database the user cannot reach answers exactly like one that does not exist. Admin management is a small service behind `/api/users/<id>/access` and two admin-only MCP tools.

**Tech Stack:** Django 5 (`backend/accounts`, `schema`, `gate`, `approvals`, `search`, `duplicates`, `scripts`, `intake`, `iquery`, `chat`, `mcp_server`, `audit`), Next.js 15 (`frontend`), Postgres + pgvector, pytest, vitest.

**Spec:** `docs/superpowers/specs/2026-11-17-schema-access-design.md`

## Global Constraints

- Levels: `viewer` < `editor` < `approver`; `admin` is global and reaches every database, present and future, with no grants. A grant's `schema` is a managed database name or the literal `*` (all, including future ones). `admin` is never a grant.
- Effective level for a non-admin on database S: the grant for S, else the `*` grant, else none; capped by the user's global role; none for an inactive user. A specific grant overrides `*` in either direction.
- Grant writes: schema must be a managed database or `*`; role in viewer/editor/approver; refused for admin targets ("admins reach every database"); a role above the target's global role is refused with the reason. `PUT` replaces the whole list atomically, one audit entry (`access.changed`, before/after, via web or mcp); every change closes the target's live sockets (existing `session.end`).
- Hidden, not forbidden: for a user with no effective level, an existing database is indistinguishable from a missing one in **every** path (same text, same status, including the gate's "already exists" messages, propose/propose_sql "does not exist" wording and by-id lookups). A user who can read but lacks the level gets the normal role-style refusal.
- Needs per database: read = viewer; propose data/schema/raw SQL/scripts/loads/imports/merge duplicates, export with data = editor; approve/reject/revert/recheck, approve raw SQL, backups, reindex = approver; `create_schema` proposal = global approver (proposer gets an `approver` grant on the new database, capped by global role); `drop_schema`/`rename_schema`/`move_table` = approver on every database involved, for proposing and for deciding; config changes (settings, rules, roles), tokens, user management stay by global role and are never filtered by grants.
- `Change.schema` is `"master"` for database ops and config changes; authorization uses `access.affected_schemas(operations)`, never `Change.schema` alone.
- Cross-database lists filter to readable databases **inside the query** (before any top-k/limit). Counts must not leak hidden databases.
- Non-admins cannot read or write grants; the two MCP tools are admin-only and absent from every other role's `tools/list` and `/api/me` `allowed`.
- Deploy: every existing non-admin user gets a `*` grant at their current global role (no behaviour change); users created afterwards start with none. `schema` absent still means `master`.
- SQL isolation (decided): app-level only, no per-database Postgres roles. `query`/iquery refuse any plan relation in an unreachable database and refuse catalog reads that list database names (`pg_namespace`, `pg_class`/`pg_tables`, other `pg_*` catalogs beyond what the app needs, `information_schema.schemata/tables/columns`) for non-admins; raw SQL and scripts must not name a database the proposer lacks editor on, checked **before** any dry run. Accepted gap (documented): a function in a readable database that reads another one.
- Commit trailers: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01QeZw812t2TxAkdSYBuXUz1`.

## Review Focus

1. A user with no grant sees the database nowhere: lists, search, history, queue, audit, export, backups, events, error texts, by-id lookups (Tasks 4, 5, 7, 8: `test_hidden_equals_missing` over every entry point).
2. `query`, iquery, raw SQL and scripts cannot reach a database the user lacks, via qualified names, views, `schema.*`, catalog reads, `search_path` tricks (Task 6).
3. Grant above the global role never exceeds it; viewer grant cannot propose; editor cannot approve; `*` + specific override both ways (Task 1).
4. Multi-database operations need the right level on every database for proposing and deciding; `Change.schema == "master"` does not leak or hide database ops (Task 7).
5. Only an admin can read/write grants on web and MCP; non-admin tokens never list the tools; no self-grant (Tasks 2, 3).
6. Existing users keep exactly their access after migration; new users start with none; seeds and the test suite are not broken by fail-closed (Task 1).
7. No live event, diff or SQL for an unreachable database; a grant change reaches open sockets (Task 8).
8. `/api/me` and the profile match what the server enforces for the same user (Tasks 3, 9).

---

### Task 1: Grant model, access service, migration

**Files:**
- Create: `backend/accounts/access.py`, `backend/accounts/migrations/0007_schema_grants.py`, `backend/tests/test_access.py`
- Modify: `backend/accounts/models.py` (add `SchemaGrant`), `backend/tests/conftest.py`

**Interfaces:**
- Produces `accounts.models.SchemaGrant(user FK related_name="schema_grants", schema CharField(63), role CharField(16), created_at)`, unique `(user, schema)`.
- Produces in `accounts/access.py`: `ALL = "*"`; `effective_role(user, schema: str) -> str | None` (a `Role` value or None); `can(user, schema: str, level: str) -> bool`; `readable_schemas(user) -> list[str]` (managed databases the user can read, master first); `require(user, schema: str, level: str = "viewer") -> str` (returns `schema`; raises `introspect.UnknownSchema` with the same message a missing database gets when the user has no effective level or the database is not managed; raises `AccessDenied(UserManagementDenied-like, message)` carrying the role-style refusal when they can read but lack `level`); `affected_schemas(operations: list[dict], default: str = "master") -> set[str]` (reads `schema`, `name`, `new_name`, `from_schema`, `to_schema` per op kind; database ops return the databases they name); `class AccessDenied(Exception)`.
- Test helper in `tests/conftest.py`: a `post_save` hook on `User` that gives each newly created non-admin user a `*` grant at its global role, **off** for tests marked `@pytest.mark.no_default_access` (register the marker in `pytest.ini`), so the existing suite keeps passing and new tests opt out.

- [ ] **Step 1: Write failing tests in `tests/test_access.py`** (all `@pytest.mark.no_default_access`): `test_no_grant_means_no_access`; `test_star_grant_reaches_every_database_including_new_ones`; `test_specific_grant_overrides_star_both_ways` (`*` editor + `crm` viewer → viewer on crm; `*` viewer + `crm` editor → editor on crm); `test_grant_is_capped_by_global_role` (viewer global + `*` approver grant → viewer); `test_inactive_user_has_no_access`; `test_admin_reaches_everything_without_grants`; `test_require_hidden_equals_missing` (same exception text for an existing ungranted database and a nonexistent one); `test_require_denies_level_with_role_message` (viewer grant, `require(..., "editor")` raises `AccessDenied`); `test_readable_schemas_lists_only_granted`; `test_affected_schemas_for_each_database_op` (create_schema, rename_schema, drop_schema, move_table, ordinary op with and without `schema`); `test_migration_gives_existing_non_admin_users_star_grant_at_global_role` (use `MigrationExecutor` on 0006→0007 with users made before).
- [ ] **Step 2: Run** `cd backend; . /tmp/spc.sh; python -m pytest tests/test_access.py -q -p no:cacheprovider`. Expected: FAIL (import errors).
- [ ] **Step 3: Implement the model, `access.py` and migration 0007** (schema change + `RunPython` data step; reversible: reverse drops grants). Hook for tests in `conftest.py` as described.
- [ ] **Step 4: Run** `tests/test_access.py`, then the full backend suite once. Expected: all pass (the new hook keeps old tests green).
- [ ] **Step 5: Commit** `feat(access): grants model and access service`.

### Task 2: Grant management service and web endpoints

**Files:**
- Modify: `backend/accounts/services.py`, `backend/accounts/views.py`, `backend/accounts/urls.py`, `backend/tests/test_mcp_parity.py` (web side only; MCP entries come in Task 3)
- Test: `backend/tests/test_access_manage.py`

**Interfaces:**
- Consumes Task 1.
- Produces in `accounts/services.py`: `user_access(target) -> dict` (`{"grants": [{"schema","role"}], "cap": role}`); `set_user_access(actor, target, grants: list[dict], *, via: str = "web") -> dict` (admin only via the existing `_require_admin`/`NotAdmin`; validation per Global Constraints; atomic replace; audit `access.changed` with before/after and `via`; `_close_sockets_on_commit(target)`); `create_user(actor, email, role, first_name="", last_name="", access: list[dict] | None = None)` (optional initial grants through the same validator, one transaction).
- Produces routes `GET /api/users/<int:user_id>/access` and `PUT /api/users/<int:user_id>/access` (admin, body `{"grants": [...]}`, 200 with `user_access`; invalid → 400 with the reason, non-admin → 403); `GET /api/users` and user JSON gain `access` (`{"all": bool, "schemas": [...]}` summary, admin-only endpoint so no leak).

- [ ] **Step 1: Write failing tests:** `test_only_admin_reads_or_writes_access` (viewer/editor/approver → 403 on GET and PUT; target unchanged); `test_put_replaces_grants_atomically_and_audits` (before/after in the audit entry); `test_rejects_unknown_schema_bad_role_admin_target_and_above_cap` (each 400 with a reason, nothing changed); `test_star_and_specific_grants_roundtrip`; `test_grant_change_closes_target_sockets` (patch `events.close_user_sockets`); `test_create_user_with_initial_access`; `test_new_user_without_access_has_no_grants`; `test_cannot_grant_self_when_not_admin`.
- [ ] **Step 2: Run** `tests/test_access_manage.py`. Expected: FAIL.
- [ ] **Step 3: Implement** the service functions, views, routes, `access` in user JSON; add the two routes to `WEB_ONLY` temporarily with reason "MCP tools added in the next task" only if the parity test needs it to pass (Task 3 replaces them with `COVERED`).
- [ ] **Step 4: Run** the new tests plus `test_users_api.py`, `test_accounts_manage.py`, `test_mcp_parity.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(access): admins manage per-database grants on the web api`.

### Task 3: MCP admin tools, /api/me access, capabilities

**Files:**
- Modify: `backend/mcp_server/tools.py`, `backend/mcp_server/app.py`, `backend/chat/toolspecs.py`, `backend/accounts/capabilities.py`, `backend/accounts/views.py` (`me`), `backend/tests/test_mcp_parity.py`, `backend/tests/test_mcp_tools_registry.py`, `backend/tests/test_users_api.py`
- Test: `backend/tests/test_mcp_access_tools.py`

**Interfaces:**
- Consumes Task 2 services.
- Produces tools `get_user_access(user, email: str) -> dict` and `set_user_access(user, email: str, grants: list[dict]) -> dict` in `tools.py`, `TOOL_ROLES[...] = "admin"`, `IMPL`, `@mcp.tool` wrappers in `app.py`, and entries in `NOT_IN_CHAT` (admin grant management is not offered in chat; update the pinned set in `test_mcp_tools_registry.py`). Errors become `ToolFailure` (unknown email → the same text `list_users`/user lookup uses today; `NotAdmin` cannot occur because the tools are hidden, but handle it).
- `/api/me` gains `access: {"all": bool, "schemas": [{"name", "role"}]}` (readable databases among the managed ones, with effective level; admin: `all` true); `capabilities.build` marks admin-only tools `allowed: false` for non-admins as today (no change in shape) and `roleSummary` mentions databases.
- Parity: replace the Task 2 web-only entries with `COVERED` (`api/users/<int:user_id>/access` ↔ `get_user_access`, `set_user_access`).

- [ ] **Step 1: Write failing tests:** `test_admin_token_lists_and_sets_access` (HTTP MCP, token of an admin, set then get, audit entry has `via: "mcp"`); `test_non_admin_tokens_never_see_access_tools` (approver/editor/viewer `tools/list` lacks both; direct call refused); `test_set_user_access_validation_matches_web`; `test_me_access_matches_effective_roles` (viewer with `crm` viewer grant, editor with `*`, admin); `test_every_tool_is_listed` and registry pins updated.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** tools, wrappers, `me.access`, parity, pins.
- [ ] **Step 4: Run** the new tests plus `test_mcp_parity.py`, `test_mcp_tools_registry.py`, `test_mcp_http.py`, `test_users_api.py`, `test_mcp_roles.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(access): admin MCP tools for grants, access in /api/me`.

### Task 4: Reads through the schema app and MCP read tools

**Files:**
- Modify: `backend/schema/http.py`, `backend/schema/views.py`, `backend/schema/introspect.py` (only if a helper is needed), `backend/mcp_server/tools.py` (all tools taking `schema`: `list_schemas`, `list_tables`, `describe_table`, `query`, `read_rows`, `profile_column`, `lookup_options`, `export_schema`, `create_backup`, `list_backups`, `match_data`, `get_import_recipe`), `backend/schema/backup.py`, `backend/schema/export.py`
- Test: `backend/tests/test_access_reads.py`

**Interfaces:**
- Consumes `access.require(user, schema, level)`, `readable_schemas`.
- Produces: `schemas` view and `list_schemas` return only readable databases (counts only for those); every view in `schema/views.py` and every MCP tool above calls `access.require` at the top with the right level (viewer for reads; editor for export with data and `table_export_csv`; approver for backups), mapping `UnknownSchema` to the existing 400/`ToolFailure` texts and `AccessDenied` to 403/role message; `backup.list_backups(user, schema=None)` filters to readable/approver databases (backups are MCP-only); `schema_param`'s `default` still means `master`, so a user without access to master gets the hidden/missing answer.

- [ ] **Step 1: Write failing tests** (`no_default_access`): `test_hidden_equals_missing_web` and `test_hidden_equals_missing_mcp` parametrized over every endpoint/tool above (compare status+body between a nonexistent name and an existing database the user has no grant for); `test_list_schemas_only_readable`; `test_viewer_grant_cannot_export_with_data`; `test_editor_grant_exports`; `test_backup_needs_approver_on_that_database`; `test_list_backups_filtered`; `test_user_without_master_gets_hidden_answer_by_default`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests plus the existing schema/mcp suites (`test_api_schemas.py`, `test_mcp_tools*.py`, `test_export_endpoint.py`, `test_backup*.py`). Expected: PASS.
- [ ] **Step 5: Commit** `feat(access): reads and exports respect database grants`.

### Task 5: Cross-database lists and by-id endpoints

**Files:**
- Modify: `backend/search/service.py`, `backend/search/views.py`, `backend/duplicates/views.py`, `backend/duplicates/service.py`, `backend/scripts/api.py`, `backend/scripts/service.py`, `backend/scripts/loads.py`, `backend/scripts/jobs.py`, `backend/intake/views.py`, `backend/intake/run.py`, `backend/iquery/views.py`, `backend/audit/views.py`, `backend/audit/models.py`, `backend/mcp_server/tools.py` (search, duplicates, loads, scripts, audit tools), `backend/chat/views.py` if it lists changes
- Test: `backend/tests/test_access_lists.py`

**Interfaces:**
- Consumes `access.readable_schemas`, `access.can`, `access.require`.
- Search: `service.search(query, top_k, schema, user=None)` adds `schema__in=readable` to the queryset **before** the top-k cut (reindex/indexer untouched); `schema=*` means all readable.
- Duplicates: `list_groups`, the table counters and `groups` view filter to readable; by-id (`group_detail`, `dismiss`, `reopen`, `merge`, `get_duplicate`) check the group's database (hidden = same "not found" as a missing id); merge needs editor, dismiss/reopen editor.
- Scripts/loads/imports/iquery: `validate`/`submit`/`begin`/`start` need editor on the **resolved** schema (including the auto-detected one from the file name) before reading any of its data; others' scripts in the approver list appear only when `report.validated_schema` is a database where the viewer is approver (own scripts always); loads and imports by id check the load's/run's schema; iquery `recall/suggest/learn/examples/example_detail` use viewer (learn viewer, examples approver on that database).
- Audit: `audit.entries(..., viewer=None)`: entries naming a database (details `schema`, `schema.export`, `table.export_csv`, `duplicates.scanned`, `load.*`, `backup.created`, `change.proposal_failed`, and `change.*` via a join to `Change` by `#id`) are shown only to a viewer who is approver on that database; entries with no database behave as today.

- [ ] **Step 1: Write failing tests:** `test_search_star_only_returns_readable_and_keeps_topk_full` (a hidden database has the best matches; result still has `top_k` hits from readable ones); `test_duplicates_counts_do_not_leak_hidden`; `test_by_id_endpoints_hidden_equals_missing` (duplicate group, script, load, import run, change-like ids per module); `test_script_autodetected_schema_needs_editor_before_dry_run` (no read happens, assert the dry-run helper is not called); `test_approver_scripts_list_limited_by_database`; `test_iquery_respects_grants`; `test_audit_filters_database_entries` (one entry per kind above, plus a config entry still visible); MCP counterparts for search/duplicates/loads/scripts/audit.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests plus `test_search.py`, `test_duplicates*.py`, `test_script*.py`, `test_intake*.py`, `test_iquery*.py`, `test_audit*.py`, `test_mcp_*`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(access): lists, search, duplicates, scripts and audit respect grants`.

### Task 6: SQL isolation for query, iquery, raw SQL and scripts

**Files:**
- Modify: `backend/schema/query.py` (`run_select(text, *, limit, timeout_ms, schema, user=None)`), `backend/iquery/memory.py` / `backend/iquery/suggest.py`, `backend/approvals/rawsql.py`, `backend/approvals/service.py` (`propose_sql`), `backend/scripts/sqlparse.py` or `backend/scripts/validate.py` (qualified-name check for script SQL), `README.md` (accepted gap)
- Create: `backend/schema/isolation.py`
- Test: `backend/tests/test_access_isolation.py`

**Interfaces:**
- Produces `schema/isolation.py`: `check_plan(conn, body: str, allowed: set[str], *, is_admin: bool) -> None` runs `EXPLAIN (VERBOSE, FORMAT JSON)` and raises `QueryError` if any relation node has a `Schema` outside `allowed` (plus `pg_catalog`/`information_schema` rules below); `catalog_names_leak(body: str) -> bool` (pglast walk of `RangeVar`s: refuses `pg_namespace`, `pg_class`, `pg_tables`, `pg_views`, `pg_roles`-style `pg_*` catalogs beyond the app's own needs and `information_schema.schemata/tables/columns` for non-admins; `pg_catalog` functions/types stay allowed); `named_schemas(sql: str) -> set[str]` (every qualified name in relations, types, functions, `COMMENT`/`ALTER` targets using pglast, extending `rawsql._Targets` beyond `RangeVar`).
- `run_select` and iquery's `memory.explains` call `check_plan` before executing; `propose_sql` and script submit call `named_schemas` and refuse any database where the proposer lacks editor **before** `rawsql.dry_run` or any script dry run, with the hidden-equals-missing text for unreachable ones.
- Documents in README: a function in a readable database that itself reads another database is not detected.

- [ ] **Step 1: Write failing tests** (`no_default_access`, two databases `master` and `crm` with data, user has `master` only): `test_query_qualified_name_in_other_database_refused`; `test_query_view_expansion_refused` (a view in master over a crm table); `test_query_catalog_listing_refused_for_non_admin` (`pg_namespace`, `pg_class`, `information_schema.tables`) and allowed for admin; `test_query_own_database_still_works`; `test_search_path_tricks_refused` (`SET`, schema-qualified function calls are not allowed through `query`'s single-SELECT rule, assert unchanged); `test_propose_sql_naming_other_database_refused_before_dry_run` (dry-run helper not called; same text as missing); `test_script_naming_other_database_refused`; `test_iquery_generated_sql_checked`; `test_known_gap_function_reading_other_database_is_not_detected` (documents the accepted gap; marked as such).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** `isolation.py` and wire it in.
- [ ] **Step 4: Run** the new tests plus `test_query*.py`, `test_rawsql*.py`, `test_script*.py`, `test_iquery*.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(access): query and raw SQL cannot reach databases the user lacks`.

### Task 7: Changes and approvals

**Files:**
- Modify: `backend/approvals/service.py` (`propose`, `propose_sql`, `propose_config` untouched except dry-run, `approve`, `reject`, `recheck`, `revert`, `can_decide`, `list_changes`, `pending_for`, `serialize` callers), `backend/approvals/views.py` (`changes`, `change_detail`, `change_action`, `queue`), `backend/mcp_server/tools.py` (change tools: `propose_change`, `propose_sql`, `get_change`, `list_changes`, `list_pending`, `approve_change`, `reject_change`, `revert_change`, `recheck_change`, `merge_duplicates`), `backend/gate/operations.py` / `backend/gate/check.py` (existence wording), `backend/chat/views.py`
- Test: `backend/tests/test_access_changes.py`

**Interfaces:**
- Consumes `access.affected_schemas`, `access.can`, `access.require`.
- Rules: propose needs editor on every affected database (`create_schema` needs **global approver**); decide (approve/reject/revert/recheck-by-others) needs approver on every affected database, and the existing own-data-change shortcut needs editor on it; `can_decide(user, change)` and `reject` use one shared database check (reject no longer duplicates the rule); config and role changes are decided by global role as today and are not filtered; `list_changes`, `pending_for`, `change_detail` and `get_change` show a database-scoped change only to a user who can read **any** of its affected databases, and a user who cannot see it gets the same not-found as a missing id; config changes visible as today; the queue lists only changes the user could decide.
- After an applied change: `create_schema` grants its proposer an `approver` grant on the new database (capped by global role) and audits `access.granted`; `rename_schema` moves that database's grants to the new name; `drop_schema` deletes them (all inside the apply transaction, audited, sockets closed for affected users).
- Gate wording: "already exists"/"does not exist" messages for `create_schema`, `rename_schema`, `move_table` must not reveal a hidden database to the proposer; propose first runs `access.require`-style hiding on every named database so a hidden target takes the missing branch.

- [ ] **Step 1: Write failing tests** (`no_default_access`): `test_propose_needs_editor_on_every_affected_database` (move_table, rename_schema, drop_schema, plain op); `test_editor_cannot_propose_create_schema_global_approver_can`; `test_decide_needs_approver_on_every_affected_database`; `test_editor_self_approval_needs_editor_on_that_database`; `test_reject_uses_same_database_rule_as_approve`; `test_revert_and_recheck_respect_grants`; `test_database_ops_with_change_schema_master_do_not_leak_or_hide` (a user with only `crm` cannot see/decide a `drop_schema crm` shown as `master`; a master-only user does not see it); `test_hidden_change_by_id_equals_missing`; `test_queue_and_history_filtered`; `test_config_changes_unfiltered_by_grants`; `test_create_schema_gives_proposer_approver_grant_capped`; `test_rename_schema_moves_grants_and_drop_removes_them`; `test_gate_messages_do_not_reveal_hidden_databases`; MCP versions of propose/decide/list/get.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests plus `test_approvals*.py`, `test_changes.py`, `test_gate_schema_ops.py`, `test_ops_move_table.py`, `test_managed_schemas_e2e.py`, `test_mcp_roles.py`, `test_mcp_history.py`. Expected: PASS (existing tests rely on the default `*` grant hook).
- [ ] **Step 5: Commit** `feat(access): changes and approvals are checked per database`.

### Task 8: Live events per database

**Files:**
- Modify: `backend/chat/events.py`, `backend/chat/consumers.py`, `backend/chat/signals.py`, `backend/accounts/services.py` (grant change already closes sockets)
- Test: `backend/tests/test_access_events.py`

**Interfaces:**
- Produces `events.schema_group(schema: str, level: str) -> str` (`schema.<name>.viewer` / `.approver`) and `events.groups_for_change(change) -> list[str]`: requester's user group, the global `APPROVERS` group **only for config/role changes**, and per affected database the approver group (and the viewer group for non-sensitive status events if the payload exists today for viewers).
- `EventsConsumer.connect` joins `user.<id>`, `APPROVERS` for global approvers (config events only now), and `schema_group(name, level)` for each database the user can read at the level they hold; the per-event re-check (`consumers.event`) closes with 4403 when the user's access to that event's database was lost.
- `signals.py` publishes via `groups_for_change`; the payload keeps its shape.

- [ ] **Step 1: Write failing tests** (channels test utilities as in `test_schemas_consumers.py`): `test_approver_socket_gets_events_only_for_their_databases`; `test_global_approver_still_gets_config_events_without_any_grant`; `test_socket_without_grant_receives_nothing_for_hidden_database`; `test_grant_change_closes_open_socket_and_reconnect_joins_new_groups`; `test_requester_still_gets_own_change_events`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests plus `test_chat*.py`, `test_schemas_consumers.py`, `test_accounts_final_review.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(access): live events are delivered per database`.

### Task 9: Frontend — Access panel and profile

**Files:**
- Modify: `frontend/lib/api.ts` (`Me.access`, `User.access`, `getUserAccess`, `setUserAccess`, `Grant`), `frontend/components/UsersView.tsx`, `frontend/components/ProfileView.tsx`, `frontend/components/AppShell.tsx` / views that gate on global role only when a database-level check is cheap (`DatabaseView`, `DuplicatesView`, export: hide actions the level does not allow, using `me.access`)
- Create: `frontend/components/AccessPanel.tsx` (+ test)
- Test: `frontend/components/AccessPanel.test.tsx`, `UsersView.test.tsx`, `ProfileView.test.tsx`

**Interfaces:**
- Consumes the Task 2/3 payloads: `GET/PUT /api/users/<id>/access` → `{grants:[{schema, role}], cap}`; `Me.access = {all: boolean, schemas: [{name, role}]}`.
- `AccessPanel({user, schemas, onSaved})`: an "All databases" row (none / viewer / editor / approver) and one row per database (inherit / none / viewer / editor / approver); options above the user's global role disabled with the reason; admin targets show "Admins reach every database" with no controls; explicit Save; confirm step when widening any entry to approver; server errors inline; same accessible naming pattern as the role select. The Add-user form uses the same picker (default none).
- `ProfileView` adds **Your databases**: each reachable database with the level, "All databases" for admins, and "Ask an admin for access to others". The database selector already uses the server list, so hidden databases disappear; deep links to a hidden database use each view's existing not-found path.

- [ ] **Step 1: Write failing tests:** `AccessPanel`: renders grants, disables levels above the cap with a reason, admin target shows no controls, Save calls `setUserAccess(id, grants)` with the full list, confirm before approver, arrow-key change alone does not save, server error shown; `UsersView`: opens the panel per user, Add user sends `access`; `ProfileView`: lists reachable databases with levels for viewer/editor/admin fixtures.
- [ ] **Step 2: Run** `cd frontend; npx vitest run components/AccessPanel.test.tsx components/UsersView.test.tsx components/ProfileView.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement** using existing UI parts and tokens; plain, sentence-case copy.
- [ ] **Step 4: Run** the three files, then full `npx vitest run`, `npm run typecheck`, `npm run build`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(access): access panel and your databases`.

### Task 10: Docs and full check

**Files:** Modify `README.md` (Access section: per-database access, levels, `*`, hidden-not-forbidden, deploy default, MCP `get_user_access`/`set_user_access`, accepted SQL gap), `singularity.yaml` comments if any apply.

- [ ] **Step 1:** Backend `pytest -q`. Expected: all pass.
- [ ] **Step 2:** Frontend `npx vitest run`, `npm run typecheck`, `npm run build`. Expected: all pass.
- [ ] **Step 3: Commit** `docs(access): per-database access`.
