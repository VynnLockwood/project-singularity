# Script Intake (Plan 1: cleanups + script intake) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let MCP/API accept a pg_dump-style SQL script file, validate it against the gate, and submit it as one approvable Change, with no raw SQL ever executed.

**Architecture:** A new `scripts` Django app stores the uploaded file, parses it with `pglast` into gate operations (DDL) and a row stream (COPY/INSERT), applies a mapping JSON, then runs the existing gate (`gate.check.run`) plus a rolled-back dry run. Large row sets stay in the stored file; a new `bulk_insert` op loads them through COPY into a temp table and INSERT…SELECT inside the executor's single transaction. The result is a normal pending `Change` (`source="script"`) that goes through the existing approval flow.

**Tech Stack:** Django, psycopg 3, pglast (new), pytest with scratch Postgres (`tests/pgsetup.py`), Next.js frontend, FastMCP-style tools in `mcp_server`.

**Spec:** `docs/superpowers/specs/2026-11-06-script-intake-database-view-erd-design.md` (sections 1, 2, 5, milestones 8.0 and 8). Plans for Part B (database view + export) and Part C (ERD) come later.

## Global Constraints

- Raw script text is never executed. Only gate ops built by our code run.
- Target schema is always `master`. Any other schema name in a script is a blocking error.
- `limits.maxScriptBytes` = 50 MB (52428800), `limits.maxScriptRows` = 200000, inline script via MCP max 20 KB, scripts expire after 7 days. Limits are tunables (approver-editable).
- Existing limits stay: `maxOpsPerProposal` 50, `maxRowsPerDataOp` 1000 (does not apply to `bulk_insert`, which has its own `maxScriptRows`).
- SQL built only with `sql.Identifier` / `sql.Literal` (see `gate/sqlgen.py`). snake_case names; PK/FK share the name `<table>_id`.
- Schema changes need an approver; data changes need editor+ proposer or approver (unchanged). A script containing any schema op is a schema change.
- Strict mode: any statement outside the allow-list (CREATE TABLE, ALTER TABLE ADD CONSTRAINT/…, CREATE INDEX, COMMENT ON, COPY … FROM stdin, INSERT) is a blocking error naming statement and line. psql meta lines (`\restrict`, `\unrestrict`, `SET`, `SELECT pg_catalog.set_config`) are ignored and listed as `ignored`.
- nginx `client_max_body_size` raised from 10m to 60m for the upload route only.

## Review Focus

- Empty script / script of only comments: clear error, no Change.
- COPY block with `\N`, tabs inside quoted text escaped as `\t`, backslash escapes, Thai (UTF-8) text: values load byte-exact.
- Row with a `refs` key that has no parent (orphan): report lists count + first 10 offenders, validation fails, nothing written.
- Same script submitted twice: second `submit` rejected (script already used) and re-validate gives same report.
- Script with CRLF line endings or UTF-8 BOM: parses the same as LF.
- Dry run fails halfway (CHECK violation on row N): transaction rolled back, DB unchanged, report names row N.
- Script expired or deleted between validate and submit: submit fails with a clear message.

---

## File Structure

- `backend/gate/operations.py` (modify): shapes for `add_check`, `set_table_comment`, `on_delete`, `bulk_insert`.
- `backend/gate/state.py`, `backend/gate/sqlgen.py` (modify): simulate and render the new ops.
- `backend/schema/executor.py`, `backend/schema/introspect.py` (modify): run `bulk_insert`; snapshot checks/comments.
- `backend/config/siconfig.py`, `backend/settings_app/tunables.py`, `singularity.yaml` (modify): new limits.
- `backend/scripts/` (create): `models.py`, `storage.py`, `sqlparse.py` (pglast → ops), `copyparse.py` (COPY/INSERT row stream), `mapping.py`, `validate.py`, `service.py`, `api.py`, `migrations/`.
- `backend/mcp_server/tools.py`, `backend/chat/toolspecs.py`, `backend/config/urls.py`, `nginx/*.conf` (modify).
- `frontend/components/ScriptsTab.tsx`, `frontend/lib/api.ts` (create/modify).
- `backend/tests/test_script_*.py`, `backend/tests/test_gate_new_ops.py` (create); fixture `backend/tests/data/mini_dump.sql` (create).

---

### Task 1: Live-DB cleanups (milestone 8.0)

**Files:** none in code. Done through the MCP against the live DB.

**Interfaces:** Produces: `auditColumns` rule with primary_key name `{table}_id` (alias `id`); all constraints/indexes snake_case.

- [ ] **Step 1:** `list_tables` + `query` on `pg_constraint` / `pg_indexes` (schema `master`) to list names matching `[A-Z]`. Expected: the camelCase list.
- [ ] **Step 2:** `propose_change` with `drop_index`/`add_index` and `drop_constraint`/`add_unique`/`add_foreign_key` (explicit snake_case `name`) to replace each. Show diff, get user OK, `approve_change`.
- [ ] **Step 3:** `get_rules`, then `propose_config_change` `update_rule` on `auditColumns`: primary_key column name `{table}_id`, aliases `["id"]`. Wait for approver.
- [ ] **Step 4:** Re-run Step 1 query. Expected: zero names matching `[A-Z]`.

### Task 2: New limits as tunables

**Files:** Modify `backend/config/siconfig.py` (DEFAULTS `limits`), `backend/settings_app/tunables.py` (TUNABLES), `singularity.yaml`. Test: `backend/tests/test_script_settings.py`.

**Interfaces:** Produces: config keys `limits.maxScriptBytes` (int, default 52428800, range 1024–209715200), `limits.maxScriptRows` (int, default 200000, range 1–2000000), `limits.maxInlineScriptBytes` (int, default 20480, range 1024–1048576).

- [ ] **Step 1:** Test `get_config()["limits"]` returns the three defaults; `tunables.apply(approver, {"limits.maxScriptRows": 10})` sets it; value `0` raises `TunableError`.
- [ ] **Step 2:** Run `pytest tests/test_script_settings.py -v`. Expected: FAIL (unknown setting).
- [ ] **Step 3:** Add the keys to DEFAULTS, TUNABLES (label, help, kind "int", min, max) and `singularity.yaml`.
- [ ] **Step 4:** Run the test. Expected: PASS. Commit `feat: script size and row limits as tunables`.

### Task 3: Gate ops `add_check`, `set_table_comment`, FK `on_delete`

**Files:** Modify `gate/operations.py`, `gate/state.py`, `gate/sqlgen.py`, `schema/introspect.py` (snapshot includes check names and table comment). Test: `backend/tests/test_gate_new_ops.py`.

**Interfaces:**
- Consumes: `parse_ops`, `apply_op`, `sqlgen.statements_for`.
- Produces: op shapes `{"op":"add_check","table":str,"name":str,"expression":str}`, `{"op":"set_table_comment","table":str,"comment":str}`, optional `"on_delete"` in {`restrict`,`cascade`,`set null`,`no action`} on `add_foreign_key` and on a column's `references`. `add_check.expression` is accepted only if `pglast` parses it as a single boolean expression referencing existing columns of that table (no subqueries, no function calls outside an allow-list `char_length, length, lower, upper, coalesce, abs`).

- [ ] **Step 1:** Tests: (a) `add_check` with `postcode ~ '^[0-9]{5}$'` on an existing table renders `ALTER TABLE … ADD CONSTRAINT … CHECK (…)` and adds name to state constraints; (b) expression with `(SELECT 1)` or `pg_sleep(1)` → gate error; (c) unknown column in expression → error; (d) `on_delete:"restrict"` renders `ON DELETE RESTRICT`, bad value → error; (e) `set_table_comment` renders `COMMENT ON TABLE`; (f) reverse ops exist (drop_constraint; previous comment).
- [ ] **Step 2:** Run `pytest tests/test_gate_new_ops.py -v`. Expected: FAIL.
- [ ] **Step 3:** Add shapes to `SCHEMA_OPS`; `apply_op` branches; sqlgen statements; snapshot fields. Expression check helper `check_expression(expr: str, columns: set[str]) -> list[str]` in `gate/operations.py` walking the pglast AST.
- [ ] **Step 4:** Run tests, plus `pytest tests -q`. Expected: all PASS.
- [ ] **Step 5:** Commit `feat(gate): add_check, set_table_comment, on_delete`.

### Task 4: `bulk_insert` op and executor path

**Files:** Modify `gate/operations.py`, `gate/state.py`, `gate/sqlgen.py`, `schema/executor.py`. Create `scripts/storage.py` stub only if needed by tests (use a tmp file path). Test: `backend/tests/test_bulk_insert.py`.

**Interfaces:**
- Produces: op `{"op":"bulk_insert","table":str,"columns":[str],"source":{"script_id":int,"stream":str},"refs":[{"column":str,"parent_table":str,"parent_key":str,"source_column":str}],"expected_rows":int}`; `executor.execute` calls `rows_provider(op) -> Iterator[tuple]` passed via a new keyword `row_source: Callable[[dict], Iterator[tuple]] | None`.
- Behavior: inside the one transaction, `CREATE TEMP TABLE` of text columns, `COPY … FROM STDIN` rows in, then `INSERT INTO t (cols) SELECT … FROM tmp JOIN parent ON parent.<parent_key> = tmp.<source_column>` per `refs`; row count must equal `expected_rows` or the transaction aborts; identity sequence is advanced with `setval` to `max(pk)`. Reverse op: `delete_rows` by captured id range.
- Gate: not capped by `maxRowsPerDataOp`; capped by `limits.maxScriptRows` (checked against `expected_rows`).

- [ ] **Step 1:** Tests with a `province`/`district` pair: (a) 3 rows load, ids generated, FK resolved by `code`; (b) one orphan code → `ExecError`, table still empty; (c) count mismatch → rollback; (d) `expected_rows` > `maxScriptRows` → gate error; (e) after load, next normal insert gets `max+1` id.
- [ ] **Step 2:** Run `pytest tests/test_bulk_insert.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement shape, state check (table + columns exist), executor branch.
- [ ] **Step 4:** Run tests. Expected: PASS. Commit `feat(executor): bulk_insert via COPY and temp table`.

### Task 5: `scripts` app: model and storage

**Files:** Create `backend/scripts/{__init__,apps,models,storage}.py`, migration; modify `config/settings.py` INSTALLED_APPS. Test: `backend/tests/test_script_storage.py`.

**Interfaces:**
- Produces: `Script` model (`id`, `owner` FK User, `name`, `sha256`, `size`, `path`, `status` in `uploaded|validated|submitted|expired`, `report` JSON, `mapping` JSON, `change` FK null, `created_at`, `expires_at`). `storage.save(owner, name: str, data: bytes) -> Script` (rejects empty, > `maxScriptBytes`; strips UTF-8 BOM; normalizes CRLF to LF; files under `settings.SCRIPT_DIR`), `storage.read(script) -> bytes`, `storage.purge_expired() -> int`.

- [ ] **Step 1:** Tests: save returns Script with correct sha256; empty → `ScriptError`; oversize → `ScriptError`; CRLF+BOM input stored as clean LF; `purge_expired` removes file and marks expired.
- [ ] **Step 2:** Run. Expected: FAIL. **Step 3:** Implement. **Step 4:** PASS. Commit `feat(scripts): model and storage`.

### Task 6: SQL parser (DDL → gate ops)

**Files:** Modify `backend/requirements.txt` (add `pglast`). Create `scripts/sqlparse.py`. Test: `backend/tests/test_script_sqlparse.py`; fixture `tests/data/mini_dump.sql` (trimmed copy of the Thai dump: 2 provinces, 2 districts, 3 subdistricts, CHECK, FKs).

**Interfaces:**
- Produces: `parse_script(text: str) -> ParsedScript` with `ops: list[dict]` (gate ops in dependency order, schema names rewritten to `master`), `streams: dict[str, RowStream]` (table → columns + lazy row iterator, from COPY and INSERT), `ignored: list[str]`, `errors: list[ScriptIssue(line:int, statement:str, message:str)]`.
- Mapping rules: `CREATE TABLE` → `create_table`; `ALTER TABLE … ADD CONSTRAINT PRIMARY KEY/UNIQUE/FOREIGN KEY/CHECK` → folded into column flags or `add_unique/add_foreign_key/add_check` with `on_delete`; `CREATE INDEX` → `add_index`; `COMMENT ON TABLE/COLUMN` → comment ops; anything else (DROP, GRANT, CREATE FUNCTION, DO, schema other than source one…) → error with line number.

- [ ] **Step 1:** Tests on `mini_dump.sql`: ops include the 3 `create_table`, the FK with `on_delete:"restrict"`, `add_check` for postcode; `\restrict` lines land in `ignored`; `DROP TABLE x;` yields error with its line; empty/comment-only text → error "no statements"; CRLF text parses same as LF.
- [ ] **Step 2:** Run. FAIL. **Step 3:** Implement with `pglast.parse_sql` per statement; split COPY blocks by hand (pglast cannot read stdin data). **Step 4:** PASS. Commit `feat(scripts): parse DDL into gate ops`.

### Task 7: Row streams (COPY and INSERT) and mapping JSON

**Files:** Create `scripts/copyparse.py`, `scripts/mapping.py`. Test: `backend/tests/test_script_rows.py`.

**Interfaces:**
- Produces: `iter_copy(block: str) -> Iterator[list[str | None]]` (tab split; `\N` → None; unescape `\t \n \r \\`; stops at `\.`); `apply_mapping(parsed: ParsedScript, mapping: dict) -> list[dict]` turning the table schema + mapping into ops: `create_table` using the mapping's target column names (snake_case, `<table>_id` identity PK, business-key `code` unique), then `bulk_insert` ops with `refs`. Mapping shape: `{"tables":{"<source_table>":{"target":str,"columns":{"<target_col>":"<source_col>"},"drop":[str],"refs":[{"column":str,"parent":str,"parent_key":str,"source_column":str}]}}}`.

- [ ] **Step 1:** Tests: `\N`→None; tab/newline escapes; Thai text exact; mapping `province_code→code` yields `bulk_insert` with `refs` for district→province; mapping naming a column that does not exist → issue; target name not snake_case → issue.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. Commit `feat(scripts): COPY parser and mapping`.

### Task 8: Validation report and dry run

**Files:** Create `scripts/validate.py`. Test: `backend/tests/test_script_validate.py`.

**Interfaces:**
- Consumes: `parse_script`, `apply_mapping`, `gate.check.run`, `executor.execute`, `schema.conn.rw_connection`.
- Produces: `validate(script: Script, mapping: dict | None) -> dict` report `{"ok":bool,"statements":int,"ignored":[…],"errors":[{line,statement,message}],"gate":{"blocked":[…],"warnings":[…]},"tables":[{"name","rows","orphans":int,"sample_orphans":[…]}],"dry_run":{"ok":bool,"error":str|None,"rows_loaded":{table:int}}}`. Dry run executes the ops in a transaction that is always rolled back. Report is saved on `script.report`; `script.status="validated"` only when `ok`.

- [ ] **Step 1:** Tests on `mini_dump.sql` + mapping: clean → `ok` and rows counts 2/2/3; orphan subdistrict → `ok` false, `orphans==1`, DB tables absent after; CHECK violation → dry_run error mentions row and table, DB unchanged; blocked gate rule (camelCase target name) → listed under `gate.blocked`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. Commit `feat(scripts): validation report with dry run`.

### Task 9: Submit as a Change

**Files:** Create `scripts/service.py`; modify `approvals/service.py` (`propose` accepts `source="script"`, passes `row_source`; apply path reads rows from stored file). Test: `backend/tests/test_script_submit.py`.

**Interfaces:**
- Produces: `service.upload(user, name, data) -> Script`, `service.validate(user, script_id, mapping) -> dict`, `service.submit(user, script_id, note: str = "") -> Change`. Submit only when `status=="validated"`, file sha256 unchanged, not expired, not already submitted; sets `status="submitted"`, `change`. Approval of that Change runs the ops with a `row_source` reading the stored file. Roles: viewer cannot upload; editor can upload/validate; schema-containing script needs approver to approve (existing rule).

- [ ] **Step 1:** Tests: editor submit of data-only script → Change pending then approve loads rows; script with `create_table` → editor change waits for approver; second submit → `ScriptError`; file tampered after validate → rejected; expired → rejected; viewer upload → `PermissionError`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. Commit `feat(scripts): submit script as change`.

### Task 10: REST endpoints and nginx

**Files:** Create `scripts/api.py`; modify `config/urls.py`, `nginx/*.conf`. Test: `backend/tests/test_script_api.py`.

**Interfaces:**
- Produces (all via `endpoint(...)` from `accounts/api.py`): `POST /api/scripts/` (multipart `file`, editor) → `{script_id, sha256, size}`; `POST /api/scripts/<id>/validate/` body `{mapping}` (editor) → report; `POST /api/scripts/<id>/submit/` (editor) → `{change_id}`; `GET /api/scripts/<id>/` (owner or approver) → status + report; `GET /api/scripts/` list own.

- [ ] **Step 1:** API tests with Django test client for each route, plus 401 anonymous, 403 viewer, 413 over limit, 404 other user's script.
- [ ] **Step 2:** FAIL. **Step 3:** Implement; raise nginx `client_max_body_size 60m;` in the `/api/scripts/` location only. **Step 4:** PASS. Commit `feat(scripts): REST API`.

### Task 11: MCP tools and chat tool specs

**Files:** Modify `mcp_server/tools.py` (TOOL_ROLES, IMPL), `mcp_server/app.py` (INSTRUCTIONS mention), `chat/toolspecs.py` (SPECS). Test: `backend/tests/test_script_mcp.py`.

**Interfaces:**
- Produces tools: `submit_script(name: str, content: str, mapping: dict | None)` (inline ≤ `maxInlineScriptBytes`; larger → error telling to upload via API/UI and pass `script_id`), `validate_script(script_id: int, mapping: dict | None) -> report`, `get_script(script_id: int)`. Roles: editor and up. Tool output trimmed to `errors[:20]` and `sample_orphans[:10]` to avoid oversized results.

- [ ] **Step 1:** Tests: tools listed for editor, hidden for viewer; inline over cap rejected; `validate_script` returns trimmed report; `submit_script` then `approve_change` end to end on `mini_dump.sql`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. Commit `feat(mcp): script tools`.

### Task 12: Scripts tab (frontend)

**Files:** Create `frontend/components/ScriptsTab.tsx`; modify `frontend/lib/api.ts`, `frontend/components/AppShell.tsx` (tab entry, editor+). Test: `frontend` typecheck + lint.

**Interfaces:** Consumes the REST routes of Task 10. Produces: drag-drop upload, mapping JSON editor (textarea with parse check), Validate button showing report (errors with line numbers, orphan table, dry-run result), Submit button disabled until `ok`.

- [ ] **Step 1:** Implement component with states `idle|uploaded|validated|submitted|error`.
- [ ] **Step 2:** Run `npm run typecheck && npm run lint` in `frontend`. Expected: no errors.
- [ ] **Step 3:** Manual check in dev server: upload `mini_dump.sql`, validate, submit, see change in Approvals tab. Commit `feat(frontend): Scripts tab`.

### Task 13: End-to-end with the real Thai dump

**Files:** Test: `backend/tests/test_script_thai_e2e.py` (skipped unless `THAI_DUMP` env points to the dump).

- [ ] **Step 1:** Test uploads the dump, validates with the mapping (province/district/subdistrict, `code` business keys), submits, approves; asserts counts 77 / 928 / 7720 and zero orphans.
- [ ] **Step 2:** Run with `THAI_DUMP=/mnt/user-data/uploads/e3f143e3-master-data1 pytest tests/test_script_thai_e2e.py -v`. Expected: PASS. Commit `test: thai master data end to end`.
