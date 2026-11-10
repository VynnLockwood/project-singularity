# Plan C: `propose_sql` (gated raw SQL) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One MCP/chat tool, `propose_sql`, lets an editor or approver put arbitrary SQL for a managed database in the approval queue; only an approver, in the web app, can apply it.

**Architecture:** A new kind `sql` of `Change`. `approvals/rawsql.py` owns parse, refuse, warn and dry run; `approvals/service.py` gets `propose_sql` and an `sql` branch in `approve` and `recheck`. Nothing bypasses the queue, audit or backup. The database role (`singularity_master_rw`, no rights on `singularity` or `public`, not a superuser) is the real fence; the refusal list is defence in depth.

**Tech Stack:** Django 5, psycopg 3, pglast 8.7 (`parse_sql`, `Visitor`), real Postgres in tests (`master` fixture), Next.js (ChangeCard, SettingsView).

**Spec:** `docs/superpowers/specs/2026-11-10-mcp-full-coverage-design.md` section 4 (and 5 for the instruction text, 6 for errors). Plans A and B are merged.

## Global Constraints

- Raw SQL runs as `singularity_master_rw` in ONE transaction with the executor's `LOCK_TIMEOUT` / `STATEMENT_TIMEOUT`; never as another role.
- `reason` is required, at least 10 characters.
- Defaults: `limits.maxSqlBytes` 100000, `limits.maxSqlStatements` 50, `sql.enabled` true.
- Kind is the literal `"sql"` (`Change.kind` is 8 characters wide; no migration).
- Only approvers decide kind `sql`; no MCP/chat tool approves it. `reject_change` works.
- `reverse_operations` is `None`; the card and `next` say it cannot be undone automatically.
- `check_result.violations` always has the warning `"Rules were not checked: this is raw SQL."` (severity `warn`, rule `rawSql`).
- A backup of the target schema is taken before applying; if it fails nothing runs and the change is `failed`.
- Commits: author as the owner; end each commit message with the Co-Authored-By and Claude-Session trailers from the session reminder. PR body ends with the 🤖 footer and session link.
- Test setup: `pg_ctlcluster 16 main start; . /tmp/spc.sh; cd backend; python -m pytest -q`.

## Review Focus

1. A statement that changes role or session identity slipping past the refusal list: `SET ROLE`, `SET SESSION AUTHORIZATION`, `RESET ROLE`, `SELECT set_config('role', ...)`, and the same inside `DO` (Task 2).
2. An editor approving raw SQL (REST), or any AI client approving it (`approve_change` over MCP, chat): must fail, including when the caller is an approver on MCP (Tasks 4, 6).
3. Half-applied SQL: a failing statement 3 of 4 leaves nothing; a backup failure runs nothing; a statement timeout fails the change cleanly (Task 5).
4. Dry run side effects: a dry run that inserts must not move sequences, and must not leave rows (Task 3).
5. Hostile or sloppy input: empty or comment-only SQL, a trailing `;`, a very large script, `CREATE INDEX CONCURRENTLY` (cannot run in a transaction), a statement qualified to a different schema than the chosen one, `sql.enabled` off (Tasks 2, 3, 6).

## File Structure

| File | Responsibility |
|---|---|
| `backend/approvals/rawsql.py` (new) | `Statement`, `parse_statements`, `warnings_for`, `dry_run`, `run_in_transaction` |
| `backend/schema/introspect.py` | `schema_fingerprint(schema)` |
| `backend/approvals/service.py` | `propose_sql`, `can_decide`, `approve`/`recheck` branches, `serialize` unchanged |
| `backend/config/siconfig.py`, `backend/settings_app/tunables.py` | new defaults and tunables (kind `bool`) |
| `backend/mcp_server/tools.py`, `app.py`, `backend/chat/toolspecs.py` | tool, roles, hiding, `approve_change` refusal, `_next_step`, instructions |
| `backend/search/signals.py` | reindex after an applied `sql` change |
| `frontend/components/ChangeCard.tsx`, `SettingsView.tsx` | "Raw SQL" card; checkbox for a `bool` setting |
| `README.md` | one short section |
| `backend/tests/test_rawsql_*.py`, `test_mcp_propose_sql.py` | tests |

---

### Task 1: Settings for raw SQL

**Files:**
- Modify: `backend/config/siconfig.py` (defaults), `backend/settings_app/tunables.py`, `frontend/components/SettingsView.tsx`
- Test: `backend/tests/test_rawsql_settings.py`, existing `frontend/components/SettingsView.test.tsx`

**Interfaces:**
- Produces: `get_config()["limits"]["maxSqlBytes"|"maxSqlStatements"]` (int), `get_config()["sql"]["enabled"]` (bool); tunable keys `limits.maxSqlBytes` (int 1024..5000000), `limits.maxSqlStatements` (int 1..500), `sql.enabled` (kind `"bool"`).

- [ ] **Step 1: Write failing tests** in `test_rawsql_settings.py`: defaults are `100000`, `50`, `True`; `tunables.apply(approver, {"sql.enabled": False})` stores `False` and `get_config()["sql"]["enabled"] is False`; `{"sql.enabled": "no"}` and `{"sql.enabled": 1}` raise `TunableError`; `{"limits.maxSqlStatements": 0}` and `501` raise; `null` resets to the default. Use the existing settings test fixtures for an approver.
- [ ] **Step 2: Run** `pytest tests/test_rawsql_settings.py -q`. Expected: FAIL (unknown setting / missing key).
- [ ] **Step 3: Implement** the defaults in `siconfig.py` (`"limits"` gets the two keys, new top-level `"sql": {"enabled": True}`), the three `TUNABLES` entries (label/help as in the spec; help for `sql.enabled`: "Lets editors propose raw SQL. Off hides the propose_sql tool and refuses raw SQL changes."), and a `"bool"` branch in `_clean` (accept only real `bool`). Check how `tunables` builds the settings list/`current value` for dotted keys so `sql.enabled` is read from `config["sql"]["enabled"]`.
- [ ] **Step 4: Frontend:** in `SettingsView.tsx`, render a checkbox (labelled with the setting label) for `kind === "bool"`, saving `true`/`false`; add a test in `SettingsView.test.tsx` that toggling it sends `{ "sql.enabled": false }`. Run `cd frontend && npx vitest run components/SettingsView.test.tsx`. Expected: PASS.
- [ ] **Step 5: Run** `pytest tests/test_rawsql_settings.py tests/ -q -k "settings or tunable"`. Expected: PASS.
- [ ] **Step 6: Commit** `feat(settings): raw SQL limits and switch`.

### Task 2: Parse and refuse

**Files:**
- Create: `backend/approvals/rawsql.py`
- Test: `backend/tests/test_rawsql_check.py` (pure, no database)

**Interfaces:**
- Produces:
  - `class RawSqlError(Exception)` with `.message` (what the AI can act on).
  - `@dataclass class Statement: text: str; number: int; node` (`number` is 1-based).
  - `parse_statements(sql: str, *, max_bytes: int, max_statements: int) -> list[Statement]`; raises `RawSqlError`.

- [ ] **Step 1: Write the failing tests**: (a) a two-statement script returns 2 `Statement`s whose `text` has no trailing `;` and `number` 1, 2; (b) empty, whitespace-only and comment-only input raise "no statements"; (c) syntax error message contains `position` and the statement text hint (use pglast's error location); (d) `len(sql.encode()) > max_bytes` raises mentioning `limits.maxSqlBytes`; more than `max_statements` raises mentioning `limits.maxSqlStatements`; (e) `pytest.mark.parametrize` over refused SQL, each raising with the statement number and a message naming the refused thing: `BEGIN`, `COMMIT`, `ROLLBACK`, `SAVEPOINT a`, `PREPARE TRANSACTION 'x'`, `SET ROLE postgres`, `SET SESSION AUTHORIZATION x`, `RESET ROLE`, `SELECT set_config('role', 'postgres', false)`, `COPY t TO PROGRAM 'ls'`, `COPY t FROM '/etc/passwd'`, `COPY t TO '/tmp/x'`, `DO $$ BEGIN END $$ LANGUAGE plperl`, `CREATE FUNCTION f() RETURNS int LANGUAGE c AS 'x'`, `VACUUM t`, `CREATE DATABASE d`, `DROP DATABASE d`, `ALTER SYSTEM SET work_mem = 1`, `LISTEN c`, `UNLISTEN c`, `NOTIFY c`, `LOAD 'x'`, `CREATE SCHEMA s`, `DROP SCHEMA s`, `ALTER SCHEMA s RENAME TO t`, `CREATE EXTENSION hstore`, `CREATE ROLE r`, `ALTER ROLE r SUPERUSER`, `DROP ROLE r`, `GRANT r TO s` (role membership), `CREATE INDEX CONCURRENTLY i ON t (a)`, `DROP INDEX CONCURRENTLY i`, `REINDEX INDEX CONCURRENTLY i`; (f) allowed: `CREATE FUNCTION f() RETURNS int LANGUAGE sql AS 'select 1'`, a plpgsql function with a `CREATE TRIGGER` after it, `DO $$ BEGIN END $$` (plpgsql default), `GRANT SELECT ON t TO singularity_master_ro`, `ANALYZE t`, `UPDATE t SET a = 1`, `ALTER TABLE t ADD COLUMN c int`.
- [ ] **Step 2: Run** `pytest tests/test_rawsql_check.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `parse_statements` with `pglast.parse_sql` (`RawStmt.stmt_location`/`stmt_len` slice the original text; strip whitespace and one trailing `;`). Refuse by node type as in the spec section 4.3 plus the extras in Step 1: `TransactionStmt`; `VariableSetStmt` whose `name` is `role` or `session_authorization`; any `FuncCall` named `set_config` (walk with `pglast.visitors.Visitor`, so it also catches a call nested in a subquery); `CopyStmt` with `is_program` or a `filename`; `DoStmt` and `CreateFunctionStmt` with a language other than `sql`/`plpgsql` (default language of `DO` is plpgsql); `VacuumStmt` where `is_vacuumcmd`; `CreatedbStmt`, `DropdbStmt`, `AlterSystemStmt`, `ListenStmt`, `UnlistenStmt`, `NotifyStmt`, `LoadStmt`, `CreateSchemaStmt`, `DropStmt`/`RenameStmt`/`AlterOwnerStmt` on `OBJECT_SCHEMA`, `CreateExtensionStmt`, `CreateRoleStmt`, `AlterRoleStmt`, `DropRoleStmt`, `GrantRoleStmt`, and `concurrent` on `IndexStmt`/`DropStmt`/`ReindexStmt` (message: "runs inside one transaction, so CONCURRENTLY is not possible"). Every refusal message is `f"statement {n}: {what} is not allowed in raw SQL; {what to do instead}"`, e.g. schemas: "use the create_schema / rename_schema / drop_schema operations in propose_change".
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sql): parse and refuse raw SQL statements`.

### Task 3: Warnings and dry run

**Files:**
- Modify: `backend/approvals/rawsql.py`, `backend/schema/introspect.py`
- Test: `backend/tests/test_rawsql_dryrun.py` (real Postgres, `master` fixture)

**Interfaces:**
- Consumes: `Statement`, `RawSqlError` (Task 2); `executor.LOCK_TIMEOUT`, `STATEMENT_TIMEOUT`, `executor._sequence_states`, `executor._restore_sequences`.
- Produces:
  - `warnings_for(statements: list[Statement], schema: str) -> list[dict]` (violation dicts: `{"rule","severity":"warn","message","op_index":None,"suggestion":None}`).
  - `dry_run(statements, schema: str) -> list[str]`: one diff line per statement, `"<command tag> (statement n)"`, e.g. `UPDATE 928 (statement 2)`; raises `RawSqlError("statement n: <primary message>")` on a database error.
  - `introspect.schema_fingerprint(schema: str) -> str`: hash of `fingerprint(all tables)` plus `list_views` and `list_enums`.

- [ ] **Step 1: Write failing tests**:
  - `test_dry_run_reports_tags_and_changes_nothing`: make table `t` with 3 rows via `make_table` (ops helpers); `dry_run` of `ALTER TABLE t ADD COLUMN c int; UPDATE t SET a = 'x'` returns lines starting `ALTER TABLE` and `UPDATE 3`; afterwards the table has no column `c` and `a` is unchanged.
  - `test_dry_run_error_names_the_statement`: second statement `SELECT * FROM nope` raises `RawSqlError` containing `statement 2` and `nope`.
  - `test_dry_run_does_not_move_sequences`: `INSERT INTO t (a) VALUES ('q')` dry-run twice, then a real `insert_rows` via `apply` gets `t_id` 4 (same as before the dry runs).
  - `test_dry_run_runs_in_the_chosen_schema`: unqualified `CREATE TABLE z (a int)` in `schema="crm"` (create it like `test_ops_roundtrip` does) rolls back and `crm` is unchanged; in an unqualified statement `SELECT * FROM t` resolves to `crm.t`.
  - `test_statement_timeout_is_an_error_not_a_hang`: monkeypatch `executor.STATEMENT_TIMEOUT` to `"200ms"` and dry-run `SELECT pg_sleep(2)`; raises `RawSqlError`.
  - Warning tests (pure): `DROP TABLE t` and `TRUNCATE t` each add a warning naming the statement; `DELETE FROM t` with no `WHERE` adds one; `UPDATE t SET a=1` unqualified adds "refers to t without a schema; it resolves in master" (once per table name per statement), while `UPDATE master.t SET a=1` adds none; `UPDATE other.t SET a=1` adds "touches schema other, not the chosen database master"; a CTE name is not reported as a table.
  - `test_schema_fingerprint_changes_with_a_view_and_with_a_column`.
- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement.** `dry_run`: `rw_connection()`, `SET LOCAL lock_timeout/statement_timeout` as `executor.execute` does, `SET LOCAL search_path TO <schema>` (identifier-quoted), snapshot sequences of every table of the schema (`_sequence_states`), `SAVEPOINT dry`, run each statement with `conn.execute(text)` and read `cur.statusmessage`, then `ROLLBACK TO SAVEPOINT dry`, `_restore_sequences`, rollback; also restore sequences when a statement fails. Map `psycopg.Error` to `RawSqlError(f"statement {n}: {exc.diag.message_primary}")`. `warnings_for` walks each node with a `Visitor` collecting `RangeVar` (skip names bound by `CommonTableExpr`), plus the node types for drop/truncate/delete-without-where.
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sql): dry run and warnings for raw SQL`.

### Task 4: Propose

**Files:**
- Modify: `backend/approvals/service.py`
- Test: `backend/tests/test_rawsql_propose.py`

**Interfaces:**
- Consumes: Tasks 2, 3; `get_config()`.
- Produces: `service.propose_sql(user, sql: str, reason: str, *, schema: str = "master", source: str = "mcp", conversation_id: int | None = None) -> Change`; `can_decide` returns `user.can_approve` for `kind == "sql"`.

- [ ] **Step 1: Write failing tests**: viewer gets `ChangeError("forbidden")`; `reason` of 9 characters gives `invalid` mentioning 10; unknown schema gives `invalid`; `sql.enabled` off gives `invalid` ("raw SQL is switched off"); a refused statement gives `invalid` with the Task 2 message and creates no `Change`; a valid proposal by an editor yields `kind == "sql"`, `status == "pending"`, `operations == [{"op": "raw_sql", "sql": <as given>, "reason": reason}]`, `generated_sql` is the split statements, `diff` is the dry run lines, `check_result["violations"]` contains the `rawSql` warning first, `reverse_operations is None`, `prompt == reason`, `fingerprint == introspect.schema_fingerprint(schema)`, `affected_tables == []`; a dry run error gives `invalid` and creates no `Change`; an audit entry `change.proposed` exists with `kind: "sql"`. `can_decide`: editor who requested it is False, approver True, another editor False.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** `propose_sql` (checks in the spec order: role, switch, reason, schema, then `parse_statements`, `dry_run`) and the `can_decide` change (`change.kind in ("schema", "config", "sql")`). Rules are not run; `rules_hash` stays `""`.
- [ ] **Step 4: Run.** Expected: PASS; also `pytest tests/test_approvals_*.py -q` PASS.
- [ ] **Step 5: Commit** `feat(sql): propose raw SQL as a change of kind sql`.

### Task 5: Approve, apply, recheck

**Files:**
- Modify: `backend/approvals/service.py`, `backend/approvals/rawsql.py`, `backend/search/signals.py`
- Test: `backend/tests/test_rawsql_apply.py`

**Interfaces:**
- Consumes: Tasks 3, 4; `backup_schema`, `record`.
- Produces: `rawsql.run_in_transaction(statements, schema) -> list[str]` (same lines as `dry_run`, committed); `service.approve` and `service.recheck` handle kind `sql`.

- [ ] **Step 1: Write failing tests** (approver via `service.approve`):
  - `test_apply_runs_all_statements_and_audits`: `ALTER TABLE t ADD COLUMN c int; UPDATE t SET c = 7` leaves the column and values; status `applied`, `applied_at` set, `reverse_operations is None`; audit has `change.approved`, `backup.created` and `change.applied` whose details include the full `sql` and `backup`; a `.dump` file exists in `settings.BACKUP_DIR` named for the change.
  - `test_failure_in_the_third_of_four_statements_changes_nothing`: table `t` has a unique column `code` and a row `A`. Propose four statements: `ALTER TABLE t ADD COLUMN c int; UPDATE t SET c = 1; INSERT INTO t (code) VALUES ('B'); UPDATE t SET c = 2`. After proposing (dry run passes), insert a row `B` with `apply()`, which changes only data, so the fingerprint is unchanged. Approve: status `failed`, `error` names statement 3, and `t` has no column `c`.
  - `test_backup_failure_runs_nothing`: monkeypatch `service.backup_schema` to raise `BackupError`; status `failed`, `error` starts with "backup failed, nothing was changed", table unchanged.
  - `test_stale_when_the_schema_moved`: after proposing, `apply` an `add_column`; approve returns status `stale`; `recheck` as the requester brings it back to `pending` with a new fingerprint and a fresh dry run in `diff`; approve then applies.
  - `test_recheck_that_now_fails_is_rejected`: drop the referenced table then `recheck`; status `rejected`, `check_result["errors"]` holds the message.
  - `test_editor_cannot_approve`: requester editor gets `forbidden`; `sql.enabled` off gives `ChangeError("bad_state"...)` on approve ("raw SQL is switched off") and nothing runs.
  - `test_applied_sql_change_reindexes_search`: patch `search.indexer.schedule_reindex` and assert called on commit (`django_capture_on_commit_callbacks(execute=True)`).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** In `approve`, after the status and `can_decide` checks: `if change.kind == "sql": return _approve_sql(change, user, cfg)`. `_approve_sql`: switch check; compare `change.fingerprint` with `introspect.schema_fingerprint(change.schema)` (`UnknownSchema` → stale as for other kinds) → `_mark_stale`; record `change.approved`; `backup_schema(change.schema, f"change-{pk}", retention)` (on `BackupError` → `_fail`, "backup failed, nothing was changed: ..."); re-parse `operations[0]["sql"]` (never trust stored text blindly) and `run_in_transaction`; `RawSqlError` → `_fail`; on success set `applied`, `reverse_operations=None`, record `change.applied` with `{"kind": "sql", "sql": ..., "backup": path.name, "reversible": False}`. `run_in_transaction` is `dry_run` without the savepoint and rollback, and commits. In `recheck`: kind `sql` re-parses and dry-runs `operations[0]["sql"]`, refreshes `diff`, `generated_sql`, `fingerprint`, sets `pending`, or `rejected` with `check_result` errors. `search/signals.py`: reindex also for `kind == "sql"`.
- [ ] **Step 4: Run** the file plus `pytest tests/test_approvals_*.py tests/test_search*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sql): approve, apply and recheck raw SQL changes`.

### Task 6: MCP tool, chat tool, instructions

**Files:**
- Modify: `backend/mcp_server/tools.py`, `backend/mcp_server/app.py`, `backend/chat/toolspecs.py`
- Test: `backend/tests/test_mcp_propose_sql.py`; update `tests/test_mcp_tools_registry.py` expectations only if they pin counts.

**Interfaces:**
- Consumes: `service.propose_sql`.
- Produces: tool `propose_sql(sql: str, reason: str, schema: str = "master")`, role `editor`; `tools.allowed` returns False for `propose_sql` when `get_config()["sql"]["enabled"]` is false; `approve_change` raises `ToolFailure("Raw SQL is approved by a person in the web app.")` for kind `sql`.

- [ ] **Step 1: Write failing tests**: `tools.IMPL["propose_sql"](editor, sql=..., reason=...)` returns a dict with `kind == "sql"` and `next` containing "web app" and "cannot approve" for an editor AND for an approver (both); a viewer is not `allowed`; with `sql.enabled` off nobody is `allowed`; `approve_change(approver, id)` raises `ToolFailure` with the sentence above and the change stays `pending`; `reject_change(approver, id)` works; `ToolFailure` (not a traceback) for a refused statement, message includes the statement number; `get_change` and `list_pending` serialize the change; registry tests pass (`TOOL_ROLES == IMPL`, registered with the server, chat has every tool except deciding); the instruction text mentions `propose_sql` and "only when no operation fits"; the tool description names "editor" as minimum role and "an approver approves in the web app".
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** the `tools.propose_sql` function (wraps `ChangeError` into `ToolFailure`, `reason[:2000]` is NOT truncated: pass as is, the service validates), the `TOOL_ROLES`/`IMPL` entries, the `allowed` switch check, the `_next_step` branch for kind `sql` pending (always the web-app sentence naming change `#id`), the `approve_change` guard (look up kind first), the `@mcp.tool` registration in `app.py`, the chat spec in `toolspecs.py` (same text as the MCP description via a shared entry in `tools.DESCRIPTIONS`), and the INSTRUCTIONS/description rewrite from spec section 5: prefer `propose_change` operations; `propose_sql` only when none fits, with `reason`; qualify table names with the database; never claim a pending change is applied.
- [ ] **Step 4: Run** `pytest tests/test_mcp_*.py tests/test_chat_*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(mcp): propose_sql tool`.

### Task 7: Web card, README, whole-suite check

**Files:**
- Modify: `frontend/components/ChangeCard.tsx`, `README.md`
- Test: `frontend/components/Components.test.tsx` (or a new `ChangeCardSql.test.tsx`)

- [ ] **Step 1: Write failing test**: a change with `kind: "sql"`, `operations: [{op: "raw_sql", sql: "UPDATE t SET a = 1", reason: "fix the typo in t"}]`, `sql: ["UPDATE t SET a = 1"]`, one warning, status pending, viewed by an approver: renders heading text "Raw SQL", the reason, the SQL visible without clicking "Show SQL", the text "Rules were not checked" and "cannot be undone automatically", and an enabled Approve button; viewed by an editor who requested it: no Approve button.
- [ ] **Step 2: Run** `cd frontend && npx vitest run components/`. Expected: FAIL.
- [ ] **Step 3: Implement** in `ChangeCard.tsx`: label `change.kind === "sql" ? "Raw SQL"`; for kind `sql` show the reason (from `operations[0].reason`, falling back to `prompt`), always-expanded `<pre className="sql">`, and a standing `<p className="muted small">` note: "Rules were not checked. This cannot be undone automatically; a backup is taken before it runs." Add `operations` to the `Change` type in `lib/api.ts` if it is not already typed. `canDecide` already restricts non-data kinds to approvers. README: a short "Raw SQL" section (who proposes, who approves, backup, no revert, how to switch off).
- [ ] **Step 4: Run** the whole frontend suite (`npx vitest run`, `npx tsc --noEmit`) and the whole backend suite. Expected: all PASS, none skipped for the database environment.
- [ ] **Step 5: Commit** `feat(web): Raw SQL change card`.

## Self-Review (done)

- Spec coverage: 4.1 Task 6 (+ role/reason Task 4); 4.2 Task 4; 4.3 Tasks 2, 3; 4.4 Tasks 4, 6; 4.5 Task 5; 4.6 Task 1 (+ Task 6 hiding, Task 5 refusal); 4.7 Task 7; section 5 Task 6; section 6 (errors as `ToolFailure`, nothing silent) Tasks 4, 6.
- Rulings recorded in this plan (the executor ledgers them): (1) the refusal list also covers role DDL, `CREATE EXTENSION`, `CONCURRENTLY`, schema rename/owner and `set_config` calls, because they cannot work or would defeat the dry run; (2) the dry run restores sequences of every table in the schema; (3) `reason` is also stored in `Change.prompt` so every list shows it; (4) no migration, since `Change.kind` already fits `"sql"`; (5) statements qualified to a different managed schema are allowed with a warning (the role fence, not this tool, keeps them inside managed schemas); (6) raw SQL cannot be proposed from the REST/web app in this plan (the web app only approves it).
- Type consistency: `Statement`, `RawSqlError`, `parse_statements`, `dry_run`, `run_in_transaction`, `warnings_for`, `schema_fingerprint`, `propose_sql` are named identically in every task.
