# MCP Plan B: new gated operations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 12 gated operations (`set_column_default`, `drop_column_default`, `set_not_null`, `drop_not_null`, `create_view`, `drop_view`, `create_enum`, `add_enum_value`, `drop_enum`, `delete_rows`, `upsert_rows`, `move_table`) so an AI client can do common database work without raw SQL.

**Architecture:** Each operation passes through every existing layer, in this order: shape check (`gate/operations.py`) → simulated state (`gate/state.py`) → rules (`gate/rules.py`) → SQL (`gate/sqlgen.py`) → execution and reverse (`schema/executor.py`) → one diff line (`gate.sqlgen.describe`). The simulated state gains views, enums, column defaults, unique keys and table links through a `State` dict subclass, so existing code that loops over tables keeps working.

**Tech Stack:** Django 5, psycopg 3, pglast, pytest against a real Postgres.

**Spec:** `docs/superpowers/specs/2026-11-10-mcp-full-coverage-design.md` section 3 (Plan B). Read it first; conflicts resolve against it, except the plan-time rulings below, which are deliberate and each costs one line to undo.

## Plan-time rulings (deviations from the spec; the owner may overrule)

1. **`drop_enum` is added** (`schema`, destructive, needs `reason`, no reverse). The spec reverses `create_enum` with "drop only while unused" but lists no drop operation. Cost if wrong: one extra operation to delete.
2. **Rule `careWithDeletes` (warn, type `forbidden`, ops `[delete_rows]`) replaces `hardDeleteNeedsReason` (block).** `reason` is mandatory in the operation grammar, exactly like the drop operations, so a block rule could only repeat that check. The row-count warning is added at propose time (Task 5). Cost if wrong: add one block rule row.
3. **Script intake covers only operations that have a SQL statement** in a pg_dump-style script: defaults, not-null, views, enums. `delete_rows`, `upsert_rows` and `move_table` get no intake mapping (Task 9).
4. **`upsert_rows.columns`** means "the columns overwritten when the key already exists". It must be a non-empty subset of the row columns and must not contain a key column.
5. **`move_table` also refuses a table that has foreign keys to or from other tables of its schema**, because the per-schema model cannot represent a cross-schema foreign key. Spec only names the same-name and dependent-view refusals.

## Global Constraints

- Identifiers always go through `sql.Identifier`, values through `sql.Literal` (`lit`); a view query is the only free text rendered as SQL, and only after `check_view_query` (Task 3) passes.
- Every new op is accepted by `parse_ops` only with exactly its listed keys; unknown keys are errors (`keys(...)` helper).
- `reason` rule (`_reason`): required, non-empty text, on `drop_view`, `drop_enum`, `delete_rows`.
- Destructive ops go in `DESTRUCTIVE_OPS`: `drop_view`, `drop_enum`, `delete_rows`.
- Spec rule: `delete_rows` with an empty `where` is refused (reuse `_check_where`, which already refuses `{}` and `None` values).
- Row cap: `limits.maxRowsPerDataOp` (default 1000).
- Reverse operations are recorded as ops that pass the gate with `skip_rules=True`.
- Backend tests need `TEST_PG_ADMIN_USER=postgres DB_HOST=localhost DB_PORT=5432 DB_NAME=postgres DB_APP_USER=postgres DB_APP_PASSWORD=postgres PGPASSWORD=postgres DJANGO_DEBUG=1` and a running Postgres (`pg_ctlcluster 16 main start`). `tests/conftest.py::master` is the fixture for DB tests. The whole suite stays green after every task (it is 1000/1000 on main today).
- Commits are authored as the owner: no `Co-Authored-By` trailer.

## Review Focus

Failure modes the spec implies but ordinary happy-path tests do not exercise. Each has a test in the owning task.

1. `set_not_null` on a column that holds NULLs: the dry run fails and the message gives the NULL count (Task 2).
2. `create_view` with a second statement, `INTO`, `FOR UPDATE`, a data-modifying CTE, or a table outside the schema (`singularity.*`): refused or unreadable (Task 3).
3. `delete_rows` with `where: {}` or a `null` value: refused; a delete blocked by a restricting foreign key fails and names the constraint (Task 5).
4. `add_enum_value` then an `insert_rows` using that value in the same change: Postgres refuses; the error reaches the user as a failed change, nothing applied (Task 4).
5. `move_table` into a schema that already has a table or view of that name, or with a dependent view, or with foreign keys: refused with a message naming the cause (Task 7).

## File structure

| File | Responsibility |
|---|---|
| `gate/state.py` | `State` class and `apply_op` branches for every new op |
| `gate/operations.py` | shape checks, op sets, `check_view_query`, enum-name type acceptance |
| `gate/sqlgen.py` | SQL and diff lines |
| `gate/rules.py` | naming rule covers views and enums |
| `gate/check.py` | `move_table` validation (database ops path) |
| `schema/introspect.py` | `snapshot` returns a `State` with the new facts; `count_where`; `list_views`; `list_enums` |
| `schema/executor.py` | before-capture and reverse for each new op |
| `schema/util.py` | `pg_default_to_allowed` |
| `approvals/service.py` | propose-time delete count warning; `move_table` backup and fingerprint |
| `gate/defaults.py`, `settings_app/migrations/0003_care_with_deletes.py` | the new rule, additive for existing installs |
| `mcp_server/*`, `chat/toolspecs.py`, `README.md` | descriptions and visibility (Task 8) |
| `scripts/sqlparse.py`, `scripts/mapping.py` | intake (Task 9) |
| `tests/test_ops_state.py` (new) and one `tests/test_ops_<group>.py` per task | tests |

---

### Task 1: State foundation

**Files:**
- Modify: `backend/gate/state.py`, `backend/schema/introspect.py`
- Create: `backend/tests/test_ops_state.py`

**Interfaces:**
- Produces: `class State(dict)` in `gate/state.py` with attributes `views: dict[str, dict]` (`{name: {"query": str, "tables": set[str]}}`) and `enums: dict[str, list[str]]` (values in order). Both default to empty. `copy.deepcopy(State)` keeps them.
- Produces: helpers `views_of(state) -> dict`, `enums_of(state) -> dict` (return `{}` for a plain dict).
- Produces: per-table entries gain `"keys": set[frozenset[str]]` (primary key and unique column sets), `"links": set[str]` (other tables joined to this one by a foreign key, either direction), and each column entry gains `"default": str | None` (the database's expression text, or the allowed value after an op).
- Produces: `introspect.snapshot(schema) -> State` filling all of the above. Existing keys (`columns`, `indexes`, `constraints`, `pk`, `comment`) are unchanged.
- Produces: `introspect.count_where(table: str, where: dict, schema: str = "master") -> int` using the read-only connection and `sqlgen._where`.

- [ ] **Step 1: Write the failing tests** in `tests/test_ops_state.py` (fixture `master`; build tables with `executor.execute` or the existing helper in `tests/test_changes.py`): `test_snapshot_reports_defaults_keys_and_links` (a table with a text default `'x'`, a unique column, a composite unique, and an FK: assert `snap["t"]["columns"]["a"]["default"]`, `snap["t"]["keys"] == {frozenset({"t_id"}), frozenset({"a"}), frozenset({"b","c"})}`, `snap["child"]["links"] == {"t"}` and `snap["t"]["links"] == {"child"}`); `test_snapshot_reports_views_and_enums` (create a view and an enum with plain SQL through the rw connection; assert `snap.views["v"]["tables"] == {"t"}` and `snap.enums["status"] == ["new", "done"]`); `test_state_survives_deepcopy_and_plain_dicts_work` (`views_of({})` is `{}`); `test_count_where` (3 rows, `where={"a": "x"}` gives the right count).
- [ ] **Step 2: Run** `pytest tests/test_ops_state.py -q`. Expected: FAIL (`State`/`views_of` not defined).
- [ ] **Step 3: Implement** `State`, `views_of`, `enums_of` in `gate/state.py`. In `introspect.snapshot` build a `State`: defaults from `COLUMNS_SQL` (`default` column), keys from `pg_constraint` contype `p`/`u` (all columns of each constraint, as a frozenset) plus unique indexes, links from `FK_SQL`, enums from `pg_type` (`typtype = 'e'`, labels ordered by `enumsortorder`) in the schema, views from `pg_class` relkind `v` with `pg_get_viewdef(oid)` and their table dependencies from `pg_depend`/`pg_rewrite`. Add the SQL constants beside the existing ones. Implement `count_where`.
- [ ] **Step 4: Run** `pytest tests/test_ops_state.py tests/test_introspect_schemas.py tests/test_changes.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(gate): state knows defaults, keys, links, views and enums`.

---

### Task 2: Column defaults and not-null

**Files:**
- Modify: `backend/gate/operations.py`, `backend/gate/state.py`, `backend/gate/sqlgen.py`, `backend/schema/executor.py`, `backend/schema/util.py`
- Create: `backend/tests/test_ops_columns.py`

**Interfaces:**
- Consumes: Task 1 `State` columns carry `default` and `nullable`.
- Produces: ops `{"op":"set_column_default","table","column","default"}` (default = JSON scalar or a member of `DEFAULT_FUNCS`, same rule as `_check_column`), `{"op":"drop_column_default","table","column"}`, `{"op":"set_not_null","table","column"}`, `{"op":"drop_not_null","table","column"}`. All four are in `SCHEMA_OPS`.
- Produces: `schema.util.pg_default_to_allowed(expr: str | None, pg_type: str) -> tuple[bool, object]` returning `(True, allowed_value_or_func)` when the database expression maps back to an allowed default (`'x'::text`→`"x"`, `5`→`5`, `true`→`true`, `now()`→`"now()"`, `CURRENT_DATE`→`"current_date"`, `gen_random_uuid()`→`"gen_random_uuid()"`), `(True, None)` when `expr` is `None` (no default), and `(False, None)` otherwise.

- [ ] **Step 1: Write the failing tests** in `tests/test_ops_columns.py`: parse tests (each op accepts its keys, rejects unknown keys and a non-scalar default); `test_set_default_state_and_sql` (render shows `ALTER TABLE "master"."t" ALTER COLUMN "a" SET DEFAULT 'x'`); `test_set_and_drop_default_apply_and_reverse` (apply `set_column_default`, `execute` returns `reverse_ops == [{"op":"drop_column_default",...}]` when there was none, or `set_column_default` with the old value; applying the reverse restores `describe_table` default); `test_default_with_unrepresentable_previous_value_is_irreversible` (previous default `nextval(...)`: `reverse_ops is None`); `test_set_not_null_fails_with_null_count` (3 NULL rows: `ApplyError` message contains `3 row(s)`); `test_set_not_null_ok_and_reverse_is_drop_not_null`; `test_drop_not_null_refused_on_primary_key` (gate error mentions primary key); `test_drop_not_null_reverse_is_set_not_null`; `test_diff_lines` (one `describe` line per op).
- [ ] **Step 2: Run** `pytest tests/test_ops_columns.py -q`. Expected: FAIL (unknown op).
- [ ] **Step 3: Implement** the shape checks in `parse_ops` (reuse `_ident`, the default check from `_check_column`; extract it to `_check_default(d, errs, where)` and call it from both). In `state.apply_op`: `need_col`; `set_column_default` sets `columns[c]["default"]`; `drop_column_default` sets it to `None`; `set_not_null` sets `nullable=False`; `drop_not_null` sets `nullable=True` and errors `"column t.c is the primary key and cannot be made nullable"` when `pk_of(state, t) == c`. In `sqlgen.statements_for`: `ALTER TABLE .. ALTER COLUMN .. SET DEFAULT <lit|func> | DROP DEFAULT | SET NOT NULL | DROP NOT NULL`; add `describe` lines (`~ set default of t.c`, `~ drop default of t.c`, `~ set t.c not null`, `~ allow nulls in t.c`). In `executor._run_op`: before running, capture `prev_default` (query `pg_get_expr(adbin, adrelid)` for the column, plus its type) for the two default ops; for `set_not_null` run `SELECT count(*) FROM t WHERE c IS NULL` first and raise `ApplyError(f"{n} row(s) in {t}.{c} are NULL; fix or remove them first")` when `n > 0`. Reverse: `set_column_default`/`drop_column_default` → via `pg_default_to_allowed` (a `None` expr → `drop_column_default`; `(False, _)` → `None`); `set_not_null`↔`drop_not_null`.
- [ ] **Step 4: Run** `pytest tests/test_ops_columns.py tests/test_gate.py tests/test_schema.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(gate): set/drop column default and not-null operations`.

---

### Task 3: Views

**Files:**
- Modify: `backend/gate/operations.py`, `backend/gate/state.py`, `backend/gate/sqlgen.py`, `backend/gate/rules.py`, `backend/schema/executor.py`
- Create: `backend/tests/test_ops_views.py`

**Interfaces:**
- Consumes: Task 1 `State.views`.
- Produces: `check_view_query(query: str) -> list[str]` in `gate/operations.py`: problems, empty when safe. The text must parse with pglast as exactly one `SelectStmt` with no `intoClause`, no `lockingClause`, and no CTE whose query is not a `SelectStmt`; it must be at most `MAX_VIEW_LEN = 10000` characters.
- Produces: ops `{"op":"create_view","name","query","comment"?}` and `{"op":"drop_view","name","reason"}`, both in `SCHEMA_OPS`; `drop_view` in `DESTRUCTIVE_OPS`.

- [ ] **Step 1: Write the failing tests** in `tests/test_ops_views.py`: `test_check_view_query` parametrized: accepts `SELECT a FROM t`, a `WITH x AS (SELECT 1) SELECT * FROM x`, `SELECT a FROM t UNION SELECT a FROM u`; refuses `SELECT 1; SELECT 2`, `SELECT a INTO x FROM t`, `SELECT a FROM t FOR UPDATE`, `WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d`, `DELETE FROM t`, empty text, text over the length cap; `test_create_view_state_sql_and_diff` (SQL starts `CREATE VIEW "master"."v" AS SELECT`; a name equal to an existing table is a gate error `already exists`); `test_naming_rule_applies_to_view_names` (default rules, name `BadName` → a `tableNaming` violation); `test_view_applies_reads_and_reverses` (apply, `query.run_select('SELECT * FROM v')` returns rows; `reverse_ops == [drop_view ... reason "revert"]`; applying the reverse removes it); `test_unqualified_names_resolve_in_the_target_schema` (create view in a second managed schema `crm` over `crm.t`); `test_view_over_the_app_schema_is_unusable` (`SELECT * FROM singularity.accounts_user` as a view query: apply fails or selecting from the view fails with permission denied; nothing is readable); `test_drop_view_needs_reason_and_has_no_reverse`; `test_drop_table_with_dependent_view_fails_cleanly` (ApplyError with the database message).
- [ ] **Step 2: Run** `pytest tests/test_ops_views.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** `check_view_query` as above; `parse_ops` shape checks (`name` identifier, `query` text passing `check_view_query`, optional text `comment`; `drop_view` `name`+`reason`). `state.apply_op`: `create_view` errors if the name is an existing table or view, records `views[name]` (tables = relation names the query references, extracted with pglast `RangeVar` walk); `drop_view` errors if missing and removes it. `sqlgen`: re-run `check_view_query` and raise `UnsafeOp` on problems (same defence as `_render_check`); render `CREATE VIEW <schema>.<name> AS <query>` and, when given, `COMMENT ON VIEW`. Because the query is unqualified text, `executor._run_op` runs `SET LOCAL search_path TO <schema>` (via `sql.Identifier`) before a `create_view` statement and `RESET search_path` after it. `drop_view` → `DROP VIEW <schema>.<name>`. `rules.evaluate` naming branch (`target == "table"`) also covers `create_view` names. Reverse: `create_view` → `drop_view` (reason `revert`); `drop_view` → `None`. `describe`: `+ create view v`, `- drop view v (reason: ...)`.
- [ ] **Step 4: Run** `pytest tests/test_ops_views.py tests/test_gate.py tests/test_schema.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(gate): create_view and drop_view operations`.

---

### Task 4: Enums

**Files:**
- Modify: `backend/gate/operations.py`, `backend/gate/state.py`, `backend/gate/sqlgen.py`, `backend/gate/rules.py`, `backend/schema/executor.py`, `backend/schema/util.py`
- Create: `backend/tests/test_ops_enums.py`

**Interfaces:**
- Consumes: Task 1 `State.enums`.
- Produces: ops `{"op":"create_enum","name","values"}`, `{"op":"add_enum_value","name","value"}`, `{"op":"drop_enum","name","reason"}`, in `SCHEMA_OPS`; `drop_enum` in `DESTRUCTIVE_OPS`.
- Produces: `ENUM_NAME_RE = ^[a-z][a-z0-9_]{0,62}\Z`. A column or `alter_column_type` `type` is accepted by `parse_ops` when it matches `TYPE_RE` **or** `ENUM_NAME_RE`; `state.apply_op` then requires a non-builtin type to be a name in `enums_of(state)` (error `unsupported type 'x': not a built-in type or an enum of this schema`).
- Produces: `sqlgen` helper `_type_sql(t: str, enums: dict, schema: str) -> sql.Composable`: the built-in type as raw allow-listed SQL, an enum as `schema.name` qualified identifiers.
- Produces: `schema.util.pg_type_to_allowed(pg_type, enums=())` extended: a type whose unqualified name is in `enums` returns that name.

- [ ] **Step 1: Write the failing tests** in `tests/test_ops_enums.py`: shape tests (values non-empty strings, unique, at most 100, each at most 63 bytes; reject empty list, duplicates, 101 values); `test_enum_as_column_type` (create_enum then create_table with that type applies; a column typed with an unknown name is a gate error); `test_add_enum_value_appends_and_rejects_duplicates`; `test_create_enum_reverse_is_drop_enum_while_unused` (apply then revert works; with a column using it the revert fails with the database message, nothing changes); `test_add_enum_value_reverse_is_none`; `test_new_value_used_in_the_same_change_is_refused_by_postgres` (add_enum_value + insert_rows using it: `execute` raises `ApplyError`, the enum has no new value afterwards); `test_alter_column_type_to_enum_reverses_to_the_old_type`; `test_enum_names_follow_the_naming_rule` (default rules: `BadEnum` gives a `tableNaming` violation); `test_drop_enum_needs_reason`.
- [ ] **Step 2: Run** `pytest tests/test_ops_enums.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Shape checks and the widened type check in `_check_column` and the `alter_column_type` branch. `state.apply_op`: `create_enum` errors if the name collides with an enum, table or view; `add_enum_value` errors if the enum is missing or the value exists; `drop_enum` errors if missing, or if any column in the state still has that type; type validation in `add_column`, `create_table`, `alter_column_type` through one helper `_need_type(t, state, err)`. `sqlgen`: `CREATE TYPE <schema>.<name> AS ENUM (<lit,...>)`, `ALTER TYPE .. ADD VALUE <lit>`, `DROP TYPE <schema>.<name>`; replace the raw `sql.SQL(normalize_type(...))` in `_col_def` and `alter_column_type` with `_type_sql(...)` (the `USING col::type` cast uses the same composable); thread `enums_of(state_before)` through. `rules.evaluate`: naming covers `create_enum` names. Reverse: `create_enum` → `drop_enum` (reason `revert`); `add_enum_value`, `drop_enum` → `None`. `describe`: `+ create enum e (a, b)`, `+ add value 'x' to enum e`, `- drop enum e (reason: ...)`.
- [ ] **Step 4: Run** `pytest tests/test_ops_enums.py tests/test_gate.py tests/test_schema.py tests/test_sqlgen_schemas.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(gate): enum types as operations and column types`.

---

### Task 5: delete_rows

**Files:**
- Modify: `backend/gate/operations.py`, `backend/gate/state.py`, `backend/gate/sqlgen.py`, `backend/gate/defaults.py`, `backend/schema/executor.py`, `backend/approvals/service.py`
- Create: `backend/settings_app/migrations/0003_care_with_deletes.py`, `backend/tests/test_ops_delete.py`

**Interfaces:**
- Consumes: `introspect.count_where` (Task 1); executor `_before_rows`-style capture.
- Produces: op `{"op":"delete_rows","table","where","reason"}` in `DATA_OPS` and `DESTRUCTIVE_OPS`. `kind_of` stays `data`.
- Produces: rule `careWithDeletes` in `DEFAULT_RULES` (type `forbidden`, params `{"ops":["delete_rows"]}`, severity `warn`).
- Produces: in `approvals.service.propose`, one extra violation per `delete_rows` op: `{"rule":"deleteRowCount","severity":"warn","message":"delete_rows on <table> will remove <N> row(s)","op_index":i,"suggestion":None}`, appended to `check_result["violations"]`. It never blocks.

- [ ] **Step 1: Write the failing tests** in `tests/test_ops_delete.py`: shape (`where` required and non-empty; `{"a": None}` refused; `reason` required); `test_delete_rows_sql_and_diff`; `test_delete_removes_rows_and_reverse_inserts_them_back` (3 rows, delete 2 by id; `reverse_ops` is one `insert_rows` with those 2 rows including their ids; applying it restores the table exactly); `test_delete_over_the_cap_is_allowed_but_irreversible` (`max_rows=2`, 5 matching rows: rows are deleted, `reverse_ops is None`); `test_delete_blocked_by_restricting_foreign_key_names_the_constraint` (child row with `on_delete` restrict: `ApplyError` message contains the constraint name, parent rows remain); `test_careWithDeletes_rule_warns` (default rules: a violation with `severity == "warn"`, change is still pending); `test_propose_adds_the_row_count_warning` (service-level: `approvals.propose(editor, [delete 2 rows])` has a `deleteRowCount` violation naming `2`); `test_migration_adds_the_rule_to_existing_installs` (delete the rule row, run the migration function, the rule exists once; run again, still once).
- [ ] **Step 2: Run** `pytest tests/test_ops_delete.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Shape check reusing `_check_where` and `_reason`. `state.apply_op`: need table and every `where` column. `sqlgen`: `DELETE FROM <t> WHERE <where>`; `describe`: `- delete rows from t where {...} (reason: ...)`. `executor._run_op` for `delete_rows`: `SELECT count(*)` with the where; when the count is at most `max_rows`, capture the full rows (`SELECT * ... FOR UPDATE`) as `jsonable` dicts and, after the delete, return `[{"op":"insert_rows","table":t,"rows":rows}]` (an empty match returns `[]`); when over the cap return `None` after deleting. `approvals.service.propose`: after `check.run`, for each `delete_rows` op call `introspect.count_where` and append the warning to `res.violations` before `_apply_check` (the same list `_apply_check` stores). Add the rule to `DEFAULT_RULES`; the migration is a `RunPython` that creates the rule through `settings_app.services` helpers only when no rule named `careWithDeletes` exists, and records a version like `seed_default_rules` does. Reverse migration is a no-op.
- [ ] **Step 4: Run** `pytest tests/test_ops_delete.py tests/test_gate.py tests/test_changes.py tests/test_rules*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(gate): delete_rows operation with undo and a row-count warning`.

---

### Task 6: upsert_rows

**Files:**
- Modify: `backend/gate/operations.py`, `backend/gate/state.py`, `backend/gate/sqlgen.py`, `backend/schema/executor.py`
- Create: `backend/tests/test_ops_upsert.py`

**Interfaces:**
- Consumes: Task 1 `keys` per table; Task 5 `delete_rows` as a reverse.
- Produces: op `{"op":"upsert_rows","table","key","columns","rows"}` in `DATA_OPS`. `key`: non-empty list of column names; `columns`: non-empty list, subset of every row's keys, disjoint from `key` (ruling 4); `rows`: non-empty list of value objects, each containing all `key` columns.

- [ ] **Step 1: Write the failing tests** in `tests/test_ops_upsert.py`: shape (missing key column in a row, `columns` overlapping `key`, empty lists); `test_key_must_match_a_unique_constraint_or_primary_key` (gate error naming the key; a composite unique works; a non-unique column fails); `test_upsert_sql` (`INSERT .. ON CONFLICT ("code") DO UPDATE SET "name" = EXCLUDED."name", "updated_at" = now()` when the table has an `updated_at` role column and it is not in `columns`); `test_upsert_inserts_and_updates` (table with 1 existing row: 1 updated, 1 inserted; `created_by` filled from the actor on insert only); `test_upsert_reverse_restores_updated_values_and_deletes_inserted_rows` (revert returns the table to its first state, using `update_rows` for old values and `delete_rows` for inserted keys; composite key gives one `delete_rows` per row); `test_upsert_over_the_cap_is_irreversible` (`max_rows` smaller than the row count: applied, `reverse_ops is None`).
- [ ] **Step 2: Run** `pytest tests/test_ops_upsert.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Shape check; `state.apply_op` validates table, columns exist, and `frozenset(key) in state[t]["keys"]` (error `key (a, b) of t is not a primary key or unique constraint`); `sqlgen` renders one `INSERT ... VALUES ... ON CONFLICT (key) DO UPDATE SET c = EXCLUDED.c[, updated_at = now()]` per row (same per-row shape as `insert_rows`, including the created_by fill), `describe`: `~ upsert N row(s) into t on (key)`. `executor._run_op` for `upsert_rows`: when `len(rows) <= max_rows`, before running select the existing rows for the key values (one query per row, `FOR UPDATE`) capturing the `columns` values, and mark the others as inserted; reverse = grouped `update_rows` (by the table's primary key when present, else by the key columns) for existing rows and `delete_rows` (reason `undo upsert`) for inserted ones; otherwise `None`.
- [ ] **Step 4: Run** `pytest tests/test_ops_upsert.py tests/test_schema.py tests/test_changes.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(gate): upsert_rows operation with undo`.

---

### Task 7: move_table

**Files:**
- Modify: `backend/gate/operations.py`, `backend/gate/check.py`, `backend/gate/sqlgen.py`, `backend/schema/executor.py`, `backend/approvals/service.py`
- Create: `backend/tests/test_ops_move_table.py`

**Interfaces:**
- Consumes: Task 1 `snapshot(schema)` (`views[...]["tables"]`, table `links`).
- Produces: op `{"op":"move_table","table","from_schema","to_schema"}`, member of `DATABASE_OPS` (so it is proposed alone and the change `schema` is `master`); schema names use `valid_schema_name`.
- Produces: `check.run(..., schema_states: dict[str, dict] | None = None)`: `{schema: snapshot}` for the two schemas a `move_table` names; `_validate_database_ops` gains the argument and checks, for `move_table`: both schemas managed and different; table exists in `from_schema`; no table or view of that name in `to_schema`; no view of `from_schema` depends on the table (message lists the view names); the table has no foreign-key `links` (ruling 5).

- [ ] **Step 1: Write the failing tests** in `tests/test_ops_move_table.py` (use `create_schema` through the same helpers as `tests/test_gate_schema_ops.py` and `tests/test_executor_schemas.py`): shape; each refusal from the interface above (one test each, assert the message names the cause); `test_move_table_sql` (`ALTER TABLE "a"."t" SET SCHEMA "b"`, using `sqlgen.statements_for(op, state, actor, conv, schema="master")`); `test_move_table_moves_rows_and_reverse_moves_back` (rows readable in the new schema through `query.run_select('SELECT * FROM b.t')`, identity sequence still works: insert after the move gets the next id; revert restores); `test_move_table_service_flow` (editor proposes: pending, kind `schema`; approver approves; both schemas are backed up, `Change.status == applied`; a change made stale by editing the table in between is marked stale: the fingerprint covers both schemas).
- [ ] **Step 2: Run** `pytest tests/test_ops_move_table.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Add to `DATABASE_OPS`; shape check. `approvals.service`: where `check.run` is called for database ops pass `schema_states` built with `introspect.snapshot` for each schema a `move_table` names (both in `propose` and in the recheck inside `approve`); extend `_schemas_fingerprint` to include both schemas for `move_table`; in the backup block back up both `from_schema` and `to_schema`. `sqlgen`: `ALTER TABLE <from>.<t> SET SCHEMA <to>`; `describe`: `~ move table t from a to b`. `executor._run_op`: database ops other than create/rename/drop schema are executed as ordinary statements; reverse is `{"op":"move_table","table":t,"from_schema":to,"to_schema":from}`. The change's own `schema` stays `master`, so `affected_tables` is empty for database ops (already the behaviour).
- [ ] **Step 4: Run** `pytest tests/test_ops_move_table.py tests/test_gate_schema_ops.py tests/test_executor_schemas.py tests/test_changes.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(gate): move_table operation`.

---

### Task 8: Visibility, descriptions and round trip

**Files:**
- Modify: `backend/schema/introspect.py`, `backend/mcp_server/tools.py`, `backend/mcp_server/app.py`, `backend/chat/toolspecs.py`, `README.md`
- Create: `backend/tests/test_ops_roundtrip.py`

**Interfaces:**
- Consumes: Task 1 `snapshot` views and enums.
- Produces: `introspect.list_views(schema) -> list[{"name","comment"}]` and `introspect.list_enums(schema) -> list[{"name","values":[str]}]`; the MCP and chat `list_tables` result gains `"views"` and `"enums"` arrays (existing keys unchanged). `describe_table` on a view name returns `None` as before.
- Produces: operation lists in the `propose_change` description (`tools.DESCRIPTIONS`), `INSTRUCTIONS` in `mcp_server/app.py`, and the operation line in `chat/toolspecs.py` mention all 12 new ops with one example each.

- [ ] **Step 1: Write the failing tests** in `tests/test_ops_roundtrip.py`: `test_every_new_op_is_listed_in_the_propose_description` (each op name occurs in `tools.DESCRIPTIONS["propose_change"]` and in the chat spec text for `propose_change`); `test_list_tables_reports_views_and_enums` (through `tools.IMPL["list_tables"]`); `test_apply_then_revert_leaves_the_schema_unchanged` (one parametrized case per reversible op: take `introspect.snapshot()` plus `list_views` and `list_enums` plus table rows before; apply through `approvals.propose/approve`, then `approvals.revert`; the same facts afterwards); `test_every_op_has_a_diff_line_and_sql` (for a valid example of each of the 12 ops, `sqlgen.describe` is not just the op name and `statements_for` returns at least one statement, except move_table which needs two schemas).
- [ ] **Step 2: Run** `pytest tests/test_ops_roundtrip.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `list_views`/`list_enums`; extend `tools.list_tables` and its chat adapter; update the three description sites; add a "Operations the gate understands" table to `README.md` (op, kind, undo yes/no).
- [ ] **Step 4: Run the whole suite**: `pytest -q`. Expected: all pass, none skipped for missing DB env.
- [ ] **Step 5: Commit** `feat(mcp): list views and enums, document the new operations`.

---

### Task 9: Script intake for defaults, not-null, views and enums

**Files:**
- Modify: `backend/scripts/sqlparse.py`, `backend/scripts/mapping.py` (only if its op retargeting is per-op-kind)
- Create: `backend/tests/test_ops_script_intake.py`

**Interfaces:**
- Consumes: the ops from Tasks 2–4; `_Translator.alter_table` (the `AT_*` command dispatch near `sqlparse.py:626`), `_map_default`, and `_Translator.statement` dispatch (the `isinstance(s, ast.…)` chain near line 900).
- Produces: pg_dump-style statements become gate ops: `ALTER TABLE t ALTER COLUMN c SET DEFAULT <const|allowed func>` → `set_column_default`; `... DROP DEFAULT` → `drop_column_default`; `... SET NOT NULL` → `set_not_null`; `... DROP NOT NULL` → `drop_not_null`; `CREATE VIEW v AS <select>` (no `OR REPLACE`, no `WITH CHECK OPTION`, no column list) → `create_view`; `DROP VIEW v` → `drop_view` with `reason: "script"`; `CREATE TYPE e AS ENUM (...)` → `create_enum`; `ALTER TYPE e ADD VALUE 'x'` (no `BEFORE`/`AFTER`, no `IF NOT EXISTS`) → `add_enum_value`. Anything else on those statement kinds keeps today's behaviour: a clear refusal with the line number.

- [ ] **Step 1: Write the failing tests** in `tests/test_ops_script_intake.py`: one small script per statement above, parsed with `parse_script`, asserting the resulting op dicts; one refusal per unsupported variant (`CREATE OR REPLACE VIEW`, `ALTER TYPE .. ADD VALUE .. BEFORE`, a default that is a function outside `DEFAULT_FUNCS`) naming the line; one end-to-end test uploading a script with a type, a table using it, a view and a default, validating and submitting it, an approver approving: everything exists afterwards.
- [ ] **Step 2: Run** `pytest tests/test_ops_script_intake.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the translations in the existing dispatch, using `_ident`, `_map_default`, and `_sql` as the other handlers do; check `scripts/mapping.py` for how ops are retargeted to a destination schema and make the new ops follow the same rule (view and enum names live in the destination schema; the view query keeps unqualified names).
- [ ] **Step 4: Run** `pytest tests/test_ops_script_intake.py tests/test_script_*.py tests/test_approvals_script_gate.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(scripts): intake for defaults, not-null, views and enums`.

---

## Self-review

- Spec coverage: all 11 listed ops plus `drop_enum` (ruling 1) have a task; enum column types (Task 4); naming rules for views and enums (Tasks 3, 4); delete rule (Task 5, ruling 2); `get_rules`/`propose_change` description (Task 8); script intake (Task 9, ruling 3); apply-then-revert round trip (Task 8).
- Types: `State`, `views_of`, `enums_of`, `count_where`, `pg_default_to_allowed`, `check_view_query`, `_type_sql`, `schema_states` are each defined in exactly one task and used only by later ones.
- Proportion: the plan lists signatures, tests and spec values; bodies are left to the implementer except where Postgres behaviour decides the design.
