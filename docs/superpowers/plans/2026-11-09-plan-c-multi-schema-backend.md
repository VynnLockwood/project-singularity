# Plan C: Multi-Schema Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A "database" is a Postgres schema. The gate, executor, introspection, rows, query, duplicates, search, scripts, MCP and chat tools all work on any managed schema (default `master`), and the new ops `create_schema`, `rename_schema`, `drop_schema` manage the schemas themselves.

**Architecture:** One change targets one schema: `Change.schema` (default `master`) is passed through `check.run`, `state`, `sqlgen`, `executor`, `introspect.snapshot(schema)`, `fingerprint(tables, schema)` and the backup. Table and data ops carry no per-op schema. Schema ops (`create_schema` / `rename_schema` / `drop_schema`) form their own change and cannot be mixed with table or data ops. The app role never gets CREATE on the database: three SECURITY DEFINER functions in `singularity` (owner-run SQL) create, rename and drop managed schemas, and the executor calls them in the same transaction as the rest.

**Tech Stack:** Django, psycopg 3, Postgres 16, pytest (scratch DBs via `backend/tests/pgsetup.py`).

**Spec:** `docs/superpowers/specs/2026-11-09-database-tab-design.md` (sections 3.1 to 3.4, 4.3, "Plan C tests").

## Deviation from the spec (ruling needed from the user)

Spec 3.3 gives every existing op an optional `schema` and allows cross-schema foreign keys. This plan uses ONE schema per change instead, and drops cross-schema FKs:

- Why: per-op schema forces every state key, every `walk`, every FK name and every diff line to be schema-qualified. That touches each gate module and each of its tests. Per-change schema changes only the entry points.
- Cost: an FK from `a.t` to `b.u` cannot be proposed. Two schemas cannot change in one approval. Neither is needed by the Database tab (Plan D works on one selected schema at a time).
- Existing behaviour is untouched: no `schema` means `master`, and `master` state keys stay unqualified.

If the user rejects this, tasks 3 to 6 need rework; say so before execution.

## Global Constraints

- Schema name rule `^[a-z][a-z0-9_]{0,62}$`; refused names: `singularity`, `public`, `information_schema`, anything starting `pg_`.
- `limits.maxSchemas` default 20, range 1 to 200 (tunable, section "limits" in `settings_app/tunables.py` and defaults in `config/siconfig.py`). Counts managed schemas including `master`.
- Managed schemas = schemas owned by `singularity_master_rw` (not `singularity`, not `public`). `master` is always managed and can never be renamed or dropped.
- `drop_schema` only for empty schemas (no tables, views, sequences, types, functions). Needs `reason`. Editors propose, approvers approve (same as other destructive ops).
- NEVER run the owner migration against the live database. Ship the SQL file and a check query only; the owner runs it after explicit confirmation.
- Commits are authored as `VynnLockwood <nolanrobinson490@gmail.com>`; NO Claude co-author, "Generated with Claude Code" or Claude-Session lines.
- Test env: `pg_ctlcluster 16 main start; . /tmp/testenv.sh`; run pytest from the worktree `backend/`. The suite has 21 pre-existing failures (baseline); no new failure may appear. DB tests skip without the testenv, so implementers must run them with it sourced.
- Identifiers go through `sql.Identifier`, values through `sql.Literal` / params; no schema name is ever formatted into SQL text.

## Review Focus

1. `rename_schema` while a pending change targets the old name: that change must go stale, not apply to a different schema or crash.
2. A name that passes the regex but is reserved or already exists (`Public`-style case games, `pg_x`, `master`, an existing schema) is refused at the gate with a readable error and by the DB function too.
3. `drop_schema` on a schema that has tables, only a sequence, or only a view is refused; dropping `master`, `singularity` or `public` is refused even if a caller bypasses the gate.
4. The app/ro/rw roles cannot create, rename or drop a schema directly; only the three functions can, and they refuse unmanaged schemas.
5. A schema with a table named like a master table (e.g. `district` in both) never mixes up state, fingerprints, search docs or duplicate groups.

---

## File Structure

- Create `db/migrations/2026-11-09-managed-schemas.sql` (owner-run: 3 functions, grants, check query as a trailing comment block).
- Modify `db/init/01-schemas-roles.sh` (same statements, for fresh installs).
- Modify `backend/tests/pgsetup.py` (apply the same functions in scratch DBs, via the init script).
- Modify `backend/gate/operations.py` (new ops, name rule), `gate/check.py`, `gate/state.py`, `gate/sqlgen.py`.
- Modify `backend/schema/introspect.py`, `executor.py`, `rows.py`, `query.py`, `backup.py`, `dump_model.py`, `views.py`.
- Modify `backend/approvals/models.py` (+ new migration `0002_change_schema.py`), `approvals/service.py`, approvals serializer/views.
- Modify `backend/duplicates/service.py`, `search/indexer.py`, `scripts/validate.py`, `scripts/rowsource.py`.
- Modify `backend/mcp_server/tools.py`, `backend/chat/toolspecs.py`.
- Modify `backend/settings_app/tunables.py`, `backend/config/siconfig.py` (`limits.maxSchemas`).
- Tests next to each module under `backend/tests/`.

---

### Task 1: Owner SQL, init script, test harness

**Files:**
- Create: `db/migrations/2026-11-09-managed-schemas.sql`
- Modify: `db/init/01-schemas-roles.sh`, `backend/tests/pgsetup.py` (only if it does not pick the new statements up from the init script)
- Test: `backend/tests/test_managed_schema_functions.py`

**Interfaces:**
- Produces (SQL, schema `singularity`, `SECURITY DEFINER`, `SET search_path = pg_catalog`, `EXECUTE` granted to `singularity_master_rw` only, revoked from PUBLIC):
  - `singularity.create_managed_schema(p_name text) RETURNS void`
  - `singularity.rename_managed_schema(p_old text, p_new text) RETURNS void`
  - `singularity.drop_managed_schema(p_name text) RETURNS void`
- Each function validates names with the regex and reserved list itself and raises with `ERRCODE 'invalid_schema_name'` / `'insufficient_privilege'` / `'dependent_objects_still_exist'`.
- Produces: `singularity.managed_schemas` view is NOT created; managed = owned by rw (query in Task 3).

- [ ] **Step 1: Write failing tests** in `test_managed_schema_functions.py` (use `pgsetup.create_scratch_db` like existing DB tests; connect with `pgsetup.connect`):
  - `test_rw_can_create_via_function`: rw runs `SELECT singularity.create_managed_schema('crm')`; schema `crm` exists, owner is `singularity_master_rw`, rw can `CREATE TABLE crm.t(id int)`, ro can `SELECT` from it (default privileges) but `INSERT` fails.
  - `test_rw_cannot_create_schema_directly`: `CREATE SCHEMA x` as rw raises `InsufficientPrivilege`; app and ro also cannot call the three functions.
  - `test_reserved_and_bad_names_refused`: for `singularity`, `public`, `pg_foo`, `information_schema`, `Crm`, `1a`, `a-b`, `''`, 64-char name: create raises; for `master` create raises (exists).
  - `test_rename`: `crm` to `sales` keeps its tables, ro grants still work; renaming `master`, `singularity`, `public` or a missing schema raises; renaming onto an existing name raises.
  - `test_drop_empty_only`: drop empty works; drop with a table, with only a sequence, with only a view each raises `dependent_objects_still_exist`; `master`, `public`, `singularity` raise.
  - `test_init_and_migration_agree`: function bodies in the `.sh` and the `.sql` file are identical after whitespace normalisation (`re.sub(r"\s+", " ", ...)` of the text between `CREATE OR REPLACE FUNCTION` and the closing `$$;` for each function).
- [ ] **Step 2: Run, expect FAIL** (`pytest backend/tests/test_managed_schema_functions.py -v`; functions do not exist).
- [ ] **Step 3: Write the SQL file**: three functions; `create` does `CREATE SCHEMA %I AUTHORIZATION singularity_master_rw`, `GRANT USAGE ON SCHEMA ... TO singularity_master_ro`, and the same two `ALTER DEFAULT PRIVILEGES FOR ROLE singularity_master_rw IN SCHEMA ...` statements `master` has (copy the exact statements from `01-schemas-roles.sh`). `rename` and `drop` first check the target is owned by rw (`pg_namespace.nspowner`), refuse `master`, and `drop` checks `pg_class`, `pg_type` (non-array, non-composite-of-table), `pg_proc` for any object in the schema. Add a trailing comment with the check query: `SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='singularity' AND proname LIKE '%managed_schema'` (expects 3 rows) and `SELECT has_database_privilege('singularity_master_rw','CURRENT_DATABASE','CREATE')` expects false.
- [ ] **Step 4: Paste the same statements into `db/init/01-schemas-roles.sh`** (inside the existing psql heredoc, after the master grants). Make `pgsetup.create_scratch_db` need no change if it runs the whole script; otherwise apply the new section there.
- [ ] **Step 5: Run the new tests and the existing DB-role tests**, expect PASS and no new failures.
- [ ] **Step 6: Commit** `feat(db): managed-schema functions and owner migration`.

---

### Task 2: Gate ops for schemas, `limits.maxSchemas`

**Files:**
- Modify: `backend/gate/operations.py`, `backend/gate/check.py`, `backend/settings_app/tunables.py`, `backend/config/siconfig.py`
- Test: `backend/tests/test_gate_schema_ops.py`

**Interfaces:**
- Produces in `gate/operations.py`: `SCHEMA_NAME_RE`, `RESERVED_SCHEMAS: frozenset[str]`, `DATABASE_OPS = {"create_schema", "rename_schema", "drop_schema"}` (added to `ALL_OPS` and to `DESTRUCTIVE_OPS` only for `drop_schema`), `valid_schema_name(name: str) -> str | None` (returns an error message or None).
- Op shapes: `create_schema {name, comment?}`, `rename_schema {schema, new_name}`, `drop_schema {schema, reason}`.
- Produces: `check.run(raw_ops, schema_state, rule_list, *, actor, max_ops, skip_rules=False, allow_bulk=False, max_script_rows=None, schema="master", managed_schemas: dict[str, int] | None = None, max_schemas: int = 20)`; `managed_schemas` maps name to table count. Consumed by Task 6.
- `kind_of(ops)` returns `"schema"` for database ops. `affected_tables(ops)` returns `[]` for them.

- [ ] **Step 1: Write failing tests**: parse accepts the three ops with good names; refuses each name from Review Focus 2 with an error naming the rule; `drop_schema` without `reason` refused; mixing `create_schema` with `create_table` in one list refused (message says schema ops must be proposed alone); `create_schema` refused when `len(managed_schemas) >= max_schemas`, when the name exists, `rename_schema` refused for `master`, for a missing source, for an existing target; `drop_schema` refused for `master`, for a missing schema, and for a schema whose table count is above zero (message includes the count); rules (`rules.evaluate`) run on database ops like on other schema ops (existing rules that look at table names simply see no tables); `limits.maxSchemas` shows up in the settings tunables with range 1 to 200 and default 20.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** parse for the three ops, the name validator, the no-mix rule in `parse_ops`, and the `check.run` branch: when ops are database ops, skip `state.validate` table checks and validate against `managed_schemas` instead; return the same result object with `generated_sql` rendered by Task 4 later (for now call `sqlgen.render` which Task 4 extends; until then the test uses `generated_sql` only after Task 4, so assert on `ok`/errors only here).
- [ ] **Step 4: Run, expect PASS; run `tests/test_gate*.py` for no regressions.**
- [ ] **Step 5: Commit** `feat(gate): create/rename/drop schema ops`.

---

### Task 3: Schema-aware introspection, dump allow-list, type refusal

**Files:**
- Modify: `backend/schema/introspect.py`, `backend/schema/dump_model.py`
- Test: `backend/tests/test_introspect_schemas.py`

**Interfaces:**
- Produces: every function gains a trailing `schema: str = "master"` parameter: `snapshot(schema)`, `list_tables(schema)`, `describe_table(name, schema)`, `graph(schema)`, `fingerprint(tables, schema)`, `external_state(state, schema)`. The catalog SQL strings take `%(schema)s` / `%s` instead of the literal `'master'`; `external_state` keys stay `"<schema>.<table>"`.
- Produces: `managed_schemas() -> list[dict]` returning `[{"name", "tables": int, "comment": str | None}]`, `master` first, rest sorted; managed means `pg_namespace.nspowner = (SELECT oid FROM pg_roles WHERE rolname = 'singularity_master_rw')` (use the ro connection). Raises `UnknownSchema` (new, in introspect) via helper `require_managed(schema: str) -> str`, used by every function above so an unmanaged name (`singularity`, `public`, `pg_catalog`) never reaches a catalog query.
- Produces: `dump_model.allowed_schemas()` returns `[s["name"] for s in introspect.managed_schemas()]`.
- Produces: `dump_model.read_model` raises `ExportError` for a column whose type is not in `pg_catalog` (user types, extension types) (deferred note from Plan B).

- [ ] **Step 1: Write failing tests** with a scratch DB where `crm` is created through the Task 1 function: `managed_schemas()` lists `master` and `crm` with table counts and never lists `singularity` or `public`; `snapshot("crm")` shows only crm tables; same table name in both schemas gives two different `fingerprint` values after altering one; `describe_table("t", "crm")` is None for a master-only table; `require_managed("public")` raises; `allowed_schemas()` includes `crm`; a column of a `CREATE TYPE crm.mood AS ENUM` raises `ExportError` on export; calls without `schema` behave exactly as before (existing introspect tests pass unchanged).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** by replacing each `n.nspname = 'master'` / `schemaname = 'master'` with a bound parameter and threading `schema`; `describe_table` replaces `format('master.%%I', ...)` with `format('%%I.%%I', schema, name)`.
- [ ] **Step 4: Run new tests plus `tests/test_introspect*.py tests/test_dump*.py tests/test_export*.py`, expect PASS.**
- [ ] **Step 5: Commit** `feat(schema): introspection per managed schema`.

---

### Task 4: State and SQL generation per schema

**Files:**
- Modify: `backend/gate/sqlgen.py`, `backend/gate/state.py` (only if `walk` needs the schema; prefer not), `backend/gate/check.py`
- Test: `backend/tests/test_sqlgen_schemas.py`

**Interfaces:**
- Produces: `sqlgen.tbl(name: str, schema: str = "master") -> sql.Composed`; `statements_for(op, state_before, actor, conv=None, schema="master")`, `plan(ops, state, actor, conv=None, schema="master")`, `render(ops, state, actor, conv=None, schema="master")`. The module constant `SCHEMA` is removed; every `tbl(t)` call inside sqlgen passes the schema.
- Database ops render as `SELECT singularity.create_managed_schema('name')`, `... rename_managed_schema('old','new')`, `... drop_managed_schema('name')` (values via `sql.Literal`). `describe(op)` gets one line each.
- Produces: `check.run(..., schema=...)` forwards `schema` to `render`; FK targets (`add_foreign_key` / `create_table` references) must exist in the same state, so cross-schema references are rejected by the existing "unknown table" error. Add a test that pins that message for `references: "master.district"` style input.

- [ ] **Step 1: Write failing tests**: with `schema="crm"` every rendered statement qualifies `"crm"."t"` and none contains `"master"`; identifier-escaping holds for a schema named with the maximum 63 characters; default call still renders `"master"."t"` (golden test copied from an existing sqlgen test); database ops render the three SELECTs exactly; FK to a table not in the crm state is refused.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Keep `Conventions` and `state.walk` untouched.
- [ ] **Step 4: Run new tests and `tests/test_sqlgen*.py tests/test_gate*.py`, expect PASS.**
- [ ] **Step 5: Commit** `feat(gate): sqlgen and check take a schema`.

---

### Task 5: Executor per schema, schema ops, reverse ops

**Files:**
- Modify: `backend/schema/executor.py`, `backend/schema/backup.py`
- Test: `backend/tests/test_executor_schemas.py`

**Interfaces:**
- Produces: `execute(ops, state, actor, *, max_rows=1000, rules, row_source=None, dry_run=False, schema="master")`. The five hard-coded `format('master.%%I', ...)` become `format('%%I.%%I', schema, table)`; `_insert_targets` and `_sequence_states` take the schema; `LOCK TABLE` uses `sqlgen.tbl(t, schema)`.
- Database ops run through `rw_connection` by executing the generated `SELECT singularity.<fn>(...)`; reverse ops: `create_schema` to `[{"op": "drop_schema", "schema": name, "reason": "undo create"}]`, `rename_schema` to the opposite rename, `drop_schema` to `None` (irreversible, like other drops that lose data; here nothing is lost but the name, still irreversible per spec).
- Produces: `backup.backup_schema(schema: str, label: str, retention_days: int = 14) -> Path` writing `<schema>-<stamp>-<label>.dump` with `--schema=<schema>`; `backup_master(label, retention_days)` stays as a thin wrapper calling `backup_schema("master", ...)`; `prune` globs `*-*.dump` only inside the folder for files matching `^[a-z][a-z0-9_]*-\d{8}T\d{6}Z-.*\.dump$`.

- [ ] **Step 1: Write failing tests** (scratch DB, real roles): create a schema through a `create_schema` change, then `create_table` + `insert_rows` + `add_column` + `add_index` with `schema="crm"` land in `crm` and not in `master`; same-named table in both schemas stays independent; FK inside crm works; `execute` of `rename_schema` moves tables; `drop_schema` on non-empty fails with `ApplyError` carrying the DB message; reverse ops of create and rename round-trip (apply reverse, state equals original); dry run with `schema="crm"` leaves sequences unchanged; `backup_schema("crm", "t")` produces a restorable dump containing only crm (check with `pg_restore -l`); existing executor tests pass unchanged.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run new tests plus `tests/test_executor*.py tests/test_backup*.py`, expect PASS.**
- [ ] **Step 5: Commit** `feat(schema): executor and backup per schema`.

---

### Task 6: Change.schema and approvals

**Files:**
- Modify: `backend/approvals/models.py`, `approvals/service.py`, approvals serializer/views (where `Change` is serialised and proposed), create `backend/approvals/migrations/0002_change_schema.py`
- Test: `backend/tests/test_approvals_schemas.py`

**Interfaces:**
- Produces: `Change.schema = models.CharField(max_length=63, default="master")`; `serialize(change)` includes `"schema"`.
- Produces: `service.propose(user, raw_ops, *, source, prompt, conversation_id, reverts=None, schema="master")`. It validates `schema` with `introspect.require_managed`, passes `introspect.snapshot(schema)`, `managed_schemas`, `max_schemas` (from `limits.maxSchemas`) and `schema` to `check.run`, fingerprints with `introspect.fingerprint(tables, schema)`. For database ops the fingerprint is a hash of `introspect.managed_schemas()` (so a rename or create elsewhere makes the change stale).
- `approve()` re-checks with the stored `change.schema`, backs up with `backup.backup_schema(change.schema, f"change-{pk}", ...)` for table changes; for database ops it backs up `master` only when dropping or renaming (cheap safety), then executes with `schema=change.schema`. `recheck`, `revert` (reverse ops inherit the schema) and the scripts call sites pass the schema too.
- `approve` of any pending change whose `schema` no longer exists marks it stale with an error, never raises.

- [ ] **Step 1: Write failing tests**: propose in `crm` stores `schema="crm"` and fingerprints only crm; approve applies there; rename `crm` to `sales` makes a pending crm change stale (Review Focus 1); `create_schema` by an editor waits for an approver, by an approver applies on approve; `drop_schema` needs reason and an approver; propose with `schema="singularity"` raises a readable error; revert of a crm change reverts in crm; default propose without `schema` creates `schema="master"` rows; migration applies on a DB with existing `Change` rows (they get `master`).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Generate the migration with `manage.py makemigrations approvals -n change_schema` and keep only that field.
- [ ] **Step 4: Run new tests plus `tests/test_approvals*.py`, expect PASS.**
- [ ] **Step 5: Commit** `feat(approvals): changes target a schema`.

---

### Task 7: Rows, query, duplicates, search, scripts per schema

**Files:**
- Modify: `backend/schema/rows.py`, `backend/schema/query.py`, `backend/duplicates/service.py`, `backend/search/indexer.py`, `backend/scripts/validate.py`, `backend/scripts/rowsource.py`, `backend/scripts/service.py`
- Test: `backend/tests/test_schemas_consumers.py`

**Interfaces:**
- Produces: `rows.*` functions that take a table gain `schema: str = "master"` (`options`, list, get, and the `_describe` helper); `sql.Identifier("master", table)` becomes `sql.Identifier(schema, table)`.
- Produces: `query.run_select(text, *, limit=500, timeout_ms=5000, schema="master")` with `SET LOCAL search_path = %s` built via `sql.Identifier(schema)` after `introspect.require_managed(schema)`.
- Produces: duplicates service functions take `schema` (default `master`); `DuplicateGroup` keeps its table name and gets a `schema` column ONLY IF the model already stores table names (check `duplicates/models.py`; add field `schema` default `master` plus migration). Search docs get `schema` in their key (`<schema>.<table>`; master docs keep today's key so existing indexes stay valid) and `build_docs(schema=None)` indexes every managed schema when None.
- Produces: script intake destination schema defaults to `master`; `validate` uses `introspect.snapshot(schema)`; `external_state(state, schema)`; `executor.execute(..., schema=schema)`.

- [ ] **Step 1: Write failing tests**: rows list/options on `crm.t` return crm data while `master.t` stays separate (Review Focus 5); `run_select("select * from t", schema="crm")` reads crm, `schema="public"` and `schema="singularity"` raise before any SQL runs; duplicates scan for a crm table finds crm duplicates only; search doc keys include the schema for non-master tables and stay unchanged for master; a script targeting `crm` creates its tables there and leaves master untouched; master defaults keep every existing test green.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run new tests plus `tests/test_rows*.py tests/test_query*.py tests/test_duplicates*.py tests/test_search*.py tests/test_scripts*.py`, expect PASS.**
- [ ] **Step 5: Commit** `feat: rows, query, duplicates, search and scripts per schema`.

---

### Task 8: API, MCP and chat tools

**Files:**
- Modify: `backend/schema/views.py` (and the URL conf that routes `/api/schema/...`), approvals views, `backend/mcp_server/tools.py`, `backend/chat/toolspecs.py`, `backend/chat/orchestrator.py` (only where it calls these)
- Test: `backend/tests/test_api_schemas.py`

**Interfaces:**
- Produces: `GET /api/schema/schemas` returns `[{"name", "tables", "comment"}]` (viewer role, same auth as the tables endpoint). `GET /api/schema/tables`, `/graph`, `/tables/<name>`, `/query`, rows endpoints and `POST` propose accept `?schema=` (body `schema` for propose); invalid or unmanaged schema returns 400 `{"error": ...}`; no param means `master`.
- Produces: MCP `list_tables(user, schema="master")`, `describe_table(user, table, schema="master")`, `run_query(user, sql, limit=None, schema="master")`, `propose_change(user, operations, prompt="", *, source="mcp", schema="master")`; new MCP tool `list_schemas(user)`. Chat `toolspecs` gets a `schema` property on the same four tools, a new `list_schemas` tool, and `_OPS` text lists `create_schema{name,comment?}, rename_schema{schema,new_name}, drop_schema{schema,reason}` with the note "schema ops go alone, in their own change".
- Docs strings say what a "database" is (a Postgres schema) so the model uses the word correctly.

- [ ] **Step 1: Write failing tests**: `/api/schema/schemas` as viewer lists `master` and `crm` with counts and never `singularity`/`public`; `?schema=crm` on tables, graph, table detail and query hits crm; `?schema=public` is 400; propose with `schema` in body stores it; export endpoint still works for `crm` (allowed_schemas now includes it); MCP `list_schemas` and chat tool dispatch work and `propose_change` forwards `schema`; toolspecs snapshot test updated to the new text.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run new tests plus the whole backend suite; compare the FAILED list with the 21-line baseline; no new entries.**
- [ ] **Step 5: Commit** `feat(api): schema param, schemas endpoint, MCP and chat tools`.

---

### Task 9: Runbook and end-to-end check

**Files:**
- Create: `docs/superpowers/runbooks/managed-schemas-migration.md`
- Test: `backend/tests/test_managed_schemas_e2e.py`

- [ ] **Step 1: Write the e2e test**: one scratch DB, as an approver: propose + approve `create_schema crm`; propose + approve `create_table` in `crm` with an FK between two crm tables and `insert_rows`; query it back with `run_select(schema="crm")`; export `crm` with data and round-trip it through psql into a fresh scratch DB (reuse Plan B's round-trip helper); rename to `sales`; try `drop_schema` (refused, non-empty); drop the table, then `drop_schema` succeeds; `master` data identical before and after (row counts and fingerprint).
- [ ] **Step 2: Run, expect PASS** (it exercises Tasks 1 to 8; fix real bugs found there, in the owning module, with their own test).
- [ ] **Step 3: Write the runbook**: what the owner runs (`psql -U <owner> -d <db> -f db/migrations/2026-11-09-managed-schemas.sql`), the two check queries and expected output, how to roll back (`DROP FUNCTION` x3), and the line "do not run this until the owner confirms". Do not run it.
- [ ] **Step 4: Commit** `test(docs): managed schemas end-to-end and runbook`.

---

## Self-Review

- Spec coverage: 3.1 model and names: Tasks 1, 2, 3. Limit: Task 2. 3.2 roles/functions/migration: Tasks 1, 9. 3.3 ops: Tasks 2, 4, 5, 6 (per-change schema, flagged above). Modules stop assuming `master`: Tasks 3, 4, 5, 7. 3.4 endpoints: Task 8. Plan C tests: create, rename, drop, non-empty, reserved, limit (Task 2), direct-create refused (Task 1), ro cannot write (Task 1), ops in non-master (Task 5), FK across schemas (refused, Task 4), executor reverse (Task 5), master unchanged (every task), introspect per schema (Task 3), search keyed by schema (Task 7). Plan B deferred notes: type refusal (Task 3), `allowed_schemas` (Task 3).
- Open spec gap: cross-schema FKs (see Deviation).
- Types: `schema: str = "master"` is the only new parameter everywhere; `managed_schemas` is `dict[str, int]` in the gate and `list[dict]` in introspect (Task 6 converts).
