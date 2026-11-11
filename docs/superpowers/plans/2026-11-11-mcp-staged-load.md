# MCP Staged Load Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An AI client with no shell can load 10k to 100k rows into an existing table over MCP (`begin_load`, `append_rows`, `finish_load`, `get_load`, `cancel_load`), ending in one gated, approvable, undoable change.

**Architecture:** CSV chunks are checked, converted to COPY text and appended to a private file tracked by a `StagedLoad` row. `finish_load` wraps the rows in a `COPY` block, stores it as a `Script`, and runs the existing `scripts.service.validate` (identity mapping, external refs) and `submit`. Everything after that (gate, dry run, `bulk_insert`, approval, backup, revert) is existing code.

**Tech Stack:** Django 5, pytest (Postgres), psycopg, the existing `scripts`, `gate`, `schema`, `approvals`, `mcp_server`, `chat` packages.

**Spec:** `docs/superpowers/specs/2026-11-11-mcp-staged-load-design.md`

**Test environment:** `pg_ctlcluster 16 main start`, then `. /tmp/spc.sh`, then `cd backend && pytest <path> -q`. Tests that touch scripts use the `script_dir` fixture pattern from `tests/test_script_validate_external_ref.py` (`settings.SCRIPT_DIR = tmp_path/...`) and `pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]`.

## Global Constraints

- Tools are editor and approver only (`TOOL_ROLES` value `"editor"`); a viewer never sees them.
- CSV rules: header-less RFC 4180, columns in `begin_load` order; unquoted empty field = NULL; quoted `""` = empty string.
- A chunk with any problem is rejected whole and stores nothing; problems carry `row` counted from the first row of the whole load (1-based).
- `seq` starts at 1 and must equal `next_seq`; resending the last accepted `seq` with identical content is a no-op success.
- Defaults: `limits.maxLoadChunkBytes` 262144, `limits.maxLoadChunkRows` 5000, `limits.maxLoadBytes` 104857600, `loads.ttlHours` 24, `loads.maxOpenPerUser` 5. Total rows are capped by the existing `limits.maxScriptRows`.
- `StagedLoad.bytes` is the truth about the file length; every read or append first truncates the file to it.
- Audit events: `load.begin`, `load.finish`, `load.cancel`. Not for `append_rows`.
- The web chat must offer the same tools (`tests/test_mcp_tools_registry.py` enforces parity); `approve_change` and friends stay out of chat as today.
- No change to approval rules, the gate, or `bulk_insert`.

## Review Focus

1. A crash or error between writing a chunk to disk and committing the DB row leaves no half chunk and the next append lands at the right place. (Task 4)
2. A chunk with embedded tab, backslash, newline, quote, unicode or a field equal to `\N` loads back exactly as sent, and `NULL` vs empty string stay distinct. (Tasks 2, 8)
3. Another user's `load_id` behaves as "does not exist" for every tool; a viewer cannot call any of them. (Tasks 3, 7)
4. `finish_load` called twice, concurrently or after cancel/expiry, never creates two changes. (Task 5)
5. A child table loaded with an external ref reports a missing parent key with the row number, not the script line, and a load into a non-master database works. (Tasks 5, 8)

---

### Task 1: Settings

**Files:**
- Modify: `backend/config/siconfig.py` (DEFAULTS), `backend/settings_app/tunables.py` (TUNABLES)
- Test: `backend/tests/test_load_settings.py`

**Interfaces:**
- Produces: `get_config()["limits"]["maxLoadChunkBytes"|"maxLoadChunkRows"|"maxLoadBytes"]` and `get_config()["loads"]["ttlHours"|"maxOpenPerUser"]` with the defaults above; the five keys are in `TUNABLES` as `limits.maxLoadChunkBytes` (int, 4096..2097152), `limits.maxLoadChunkRows` (int, 1..20000), `limits.maxLoadBytes` (int, 1048576..2147483648), `loads.ttlHours` (int, 1..168), `loads.maxOpenPerUser` (int, 1..50), each with a label and help in the style of its neighbours.

- [ ] **Step 1: Write the failing test** `test_load_settings.py`: `test_defaults` asserts the five defaults from `get_config()`; `test_tunables_have_ranges` asserts the five keys exist in `TUNABLES` with the ranges above (kind `"int"`).
- [ ] **Step 2: Run** `pytest tests/test_load_settings.py -q`. Expected: FAIL (KeyError).
- [ ] **Step 3: Add the defaults to `DEFAULTS` and the five `TUNABLES` entries.**
- [ ] **Step 4: Run** the new test plus `pytest tests/test_settings*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(loads): settings for staged loads`.

### Task 2: CSV chunk parsing, COPY text, cell checks

**Files:**
- Create: `backend/scripts/loadcsv.py`
- Test: `backend/tests/test_load_csv.py`

**Interfaces:**
- Produces:
  - `parse_csv(text: str) -> list[list[str | None]]`: RFC 4180 reader (`"` quoting, `""` escape, `,` separator, CRLF or LF rows, a quoted field may contain newlines); an unquoted empty field is `None`, a quoted empty field is `""`; a trailing newline does not make an empty row; a blank line is an error. Raises `CsvError(message, row)` (row 1-based within the chunk) for an unterminated quote, a quote inside an unquoted field, or text after a closing quote.
  - `to_copy_line(row: list[str | None]) -> str`: tab-joined, `None` -> `\N`, escapes `\` `\t` `\n` `\r` as `\\ \t \n \r` (so a value `\N` becomes `\\N`), ends without newline.
  - `check_cells(rows, columns: list[dict], enums: dict[str, list[str]], first_row: int) -> list[dict]`: `columns[i]` is `{name, type, nullable, required_value: bool}` (`required_value` true when NULL is not allowed); returns up to 20 problems `{row, column, value, message}` with `row = first_row + index`. Checks: field count equals `len(columns)` (one problem for the row), NOT NULL, integer types (`smallint|integer|bigint`, optional sign, digits only), `numeric|real|double precision` (`Decimal`-parsable, no `nan` unless `real|double precision`), `boolean` (`t f true false yes no on off 1 0`, any case), `uuid`, enum membership against `enums[type]`, `varchar(n)` length <= n, `date` and `timestamp`/`timestamptz` as ISO 8601 (`YYYY-MM-DD`, optional `T` or space time `HH:MM[:SS[.ffffff]]`, optional `Z` or `+HH[:MM]`; the message names the accepted format). Any other type passes. An empty-string cell on a non-text type is treated as NULL (matching the executor).

- [ ] **Step 1: Write failing tests** in `test_load_csv.py`:
  - `test_null_vs_empty`: `parse_csv('a,,"",b\n')` == `[["a", None, "", "b"]]`.
  - `test_quotes_and_newlines`: `'"x,""y""\nz",2\n'` -> `[['x,"y"\nz', "2"]]`.
  - `test_crlf_and_no_trailing_newline`.
  - `test_csv_errors` (parametrized: unterminated quote, `ab"c`, `"a"b`, blank line) each raises `CsvError` with the right `row`.
  - `test_copy_line_escapes`: `to_copy_line(["a\tb", "c\\d", "e\nf", None, "\\N", "é"]) == "a\\tb\tc\\\\d\te\\nf\t\\N\t\\\\N\té"`.
  - `test_copy_roundtrip`: for that same row, `scripts.copyparse.unescape_field` of each non-NULL field returns the original value.
  - `test_cells`: one parametrized case per type above, good and bad, asserting the `row`/`column`/`message` of the first problem and that `first_row` offsets `row`.
  - `test_problem_cap`: 50 bad rows return 20 problems.
- [ ] **Step 2: Run** `pytest tests/test_load_csv.py -q`. Expected: FAIL (module not found).
- [ ] **Step 3: Implement the three functions** and `CsvError(Exception)` with attributes `message`, `row`. `parse_csv` is a character state machine (the `csv` module cannot tell `""` from empty).
- [ ] **Step 4: Run** the test. Expected: PASS.
- [ ] **Step 5: Commit** `feat(loads): CSV chunk parsing, COPY text and cell checks`.

### Task 3: StagedLoad model, begin, get, cancel

**Files:**
- Create: `backend/scripts/loads.py`, migration under `backend/scripts/migrations/` (generated)
- Modify: `backend/scripts/models.py`, `backend/scripts/storage.py` (add `loads_dir() -> str`, creating `SCRIPT_DIR/loads` privately with the same checks as `_ensure_dir`)
- Test: `backend/tests/test_load_begin.py`

**Interfaces:**
- Consumes: `introspect.require_managed`, `introspect.snapshot(schema)` (state `[table]["columns"][col] = {type, nullable, default}`, `["pk"]`, `.enums`), `gate.state.role_of(state, table, role)` for audit columns, `audit.models.record`, `scripts.storage.ScriptError`.
- Produces:
  - Model `StagedLoad(owner FK, schema, table, columns JSON list[str], refs JSON list[dict], status {open,finished,cancelled,expired}, next_seq int default 1, rows int default 0, bytes bigint default 0, last_chunk_sha str(64) blank, path str, script FK(Script) null, change FK(approvals.Change) null, created_at, expires_at)`.
  - `class LoadError(Exception)`.
  - `begin(user, table: str, columns: list[str], schema: str = "master", refs: list[dict] | None = None) -> dict` returning `{load_id, table, schema, columns, next_seq: 1, limits: {chunk_bytes, chunk_rows, total_rows, total_bytes, expires_at}}`. `refs` items are `{column, parent_table, parent_key, source_column}`. Rules: editor+ (`PermissionError` otherwise); table exists; `columns` non-empty, distinct, each either a real column of the table or a ref `source_column`; every `ref.column` is a real column of the table and is not also listed in `columns`; every `ref.source_column` is in `columns`; every NOT NULL column with no default that is not the pk and not an audit-role column (`role_of`) is either in `columns` or a `ref.column`, else `LoadError` naming the missing ones; `ref.parent_table` exists and `parent_key` is a single-column unique key of it (the mapping layer re-checks); open loads per user < `loads.maxOpenPerUser`. Writes audit `load.begin`.
  - `get(user, load_id: int) -> dict` (`{load_id, status, schema, table, columns, next_seq, total_rows, total_bytes, expires_at, change_id}`); `cancel(user, load_id) -> dict` (status `cancelled`, file deleted, audit `load.cancel`). Both treat another user's load as "does not exist" unless the user is an approver; `cancel` on a finished load is `LoadError`.

- [ ] **Step 1: Write failing tests** in `test_load_begin.py`: `test_begin_returns_limits`; `test_unknown_table`, `test_unknown_column`, `test_duplicate_column`; `test_missing_required_column_names_it` (a table with a NOT NULL `name_en` without default); `test_ref_source_column_may_be_extra`; `test_ref_column_not_in_columns`; `test_open_load_cap` (set `loads.maxOpenPerUser` to 1 via the settings override helper used in `test_script_settings.py`); `test_viewer_refused`; `test_other_users_load_is_missing` and `test_approver_can_read_any`; `test_cancel_removes_file_and_is_final`; `test_audit_events`.
- [ ] **Step 2: Run** `pytest tests/test_load_begin.py -q`. Expected: FAIL.
- [ ] **Step 3: Add the model and migration (`python manage.py makemigrations scripts`), `storage.loads_dir`, and `begin/get/cancel`.** The staging file name is `<uuid4 hex>.copy` under `loads_dir()`, created empty (mode 0600) at `begin`.
- [ ] **Step 4: Run** the new tests and `pytest tests/test_script_storage.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(loads): StagedLoad model, begin, get, cancel`.

### Task 4: append_rows

**Files:**
- Modify: `backend/scripts/loads.py`
- Test: `backend/tests/test_load_append.py`

**Interfaces:**
- Consumes: Task 2 functions; Task 3 model; `introspect.snapshot` for column types.
- Produces: `append(user, load_id: int, seq: int, csv_text: str) -> dict`.
  - Success: `{"ok": True, "stored": True, "rows_in_chunk": n, "total_rows": m, "next_seq": k}`; an identical resend of the last accepted seq returns the same with `"duplicate": True` and changes nothing.
  - Rejected chunk (parse or cell problems): `{"ok": False, "stored": False, "problems": [...]}` and nothing changes (`next_seq`, `rows`, file untouched).
  - Raises `LoadError` for: not owner/missing, status not `open`, expired, `seq` not the expected one (message says `expected seq N`), chunk over `maxLoadChunkBytes` (checked on the encoded length before parsing) or `maxLoadChunkRows`, total rows over `limits.maxScriptRows`, total bytes over `limits.maxLoadBytes`, an empty chunk.
  - Under `select_for_update` on the load row: truncate the file to `load.bytes`, append the COPY text (one line per row plus newline), `flush`+`fsync`, then save `rows`, `bytes`, `next_seq`, `last_chunk_sha` (sha256 of `csv_text`), `expires_at = now + ttlHours`. Column metadata comes from `snapshot(load.schema)`; a table or column that has vanished is `LoadError`.

- [ ] **Step 1: Write failing tests** in `test_load_append.py`: `test_three_chunks_in_order`; `test_retry_same_seq_same_content_is_noop` (file size unchanged); `test_same_seq_different_content_errors`; `test_skipped_seq_errors_and_says_expected`; `test_bad_chunk_stores_nothing_and_numbers_rows_across_chunks` (second chunk's bad row reports `row` continuing from chunk one); `test_file_longer_than_bytes_is_cut_back` (append garbage to the file by hand, then append a real chunk, then read the file back: no garbage, row count right) , `test_crash_between_write_and_commit` (monkeypatch `StagedLoad.save` to raise once after the file write; the next successful append of the same seq yields a clean file); `test_limits` (chunk bytes, chunk rows, total rows via a lowered `maxScriptRows`, total bytes); `test_closed_and_expired_loads_refuse` ; `test_expiry_is_extended_by_append`; `test_special_characters_are_stored_escaped` (tab, backslash, newline, `\N`, unicode; compare file lines with `to_copy_line`).
- [ ] **Step 2: Run** `pytest tests/test_load_append.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement `append`.** Do the cheap refusals before locking; do all parsing and cell checks before touching the file.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(loads): append_rows with seq, retries and crash safety`.

### Task 5: finish

**Files:**
- Modify: `backend/scripts/loads.py`
- Test: `backend/tests/test_load_finish.py`

**Interfaces:**
- Consumes: `scripts.storage.save(owner, name, data) -> Script`, `scripts.service.validate(user, script_id, mapping, schema) -> report dict`, `scripts.service.submit(user, script_id, note) -> Change`, `scripts.service`'s `storage.ScriptError`, `audit.models.record`.
- Produces: `finish(user, load_id: int, note: str = "") -> dict`.
  - Under `select_for_update` on the load: if `status == "finished"` return the stored result (`{load_id, script_id, change_id, report: <stored trimmed report>}`) without doing anything; non-open statuses otherwise refuse; zero rows refuse; the table and every real column still exist.
  - Script text: first line `COPY <schema>.staged_load (<columns joined by ", ">) FROM stdin;`, then the staged lines, then `\.` and a newline. (Use the schema-qualified form that `tests/test_script_validate_external_ref.py` uses; if the parser refuses a non-master schema, rule on the smallest fix and ledger it.) Saved with `storage.save(user, f"load-{load.pk}-{table}.sql", data)`.
  - Mapping: `{"tables": {"staged_load": {"target": table, "columns": {c: c for each real column in load.columns}, "refs": [{"column", "parent": parent_table, "parent_key", "source_column", "external": True} ...]}}}` (omit `refs` when empty).
  - Validate; if `report["ok"]` is false return `{"ok": False, "load_id", "report": <trimmed>}` with every row-bearing number converted from script line to load row (`row = line - 1`: `errors[*].line` of kind `rows`, and `tables[*].sample_orphans[*].line`, set as `row`, `line` removed), leave the load open, do not submit.
  - If ok: `submit`, set `status=finished`, `script`, `change`, delete the staging file, audit `load.finish` (details: table, schema, rows, change id), return `{"ok": True, "load_id", "script_id", "change_id", "report": <trimmed>}`.
  - A trimmed report uses the same trimming as `mcp_server.tools._trim_report` (move that function to `scripts/service.py` as `trim_report` and import it in `tools.py`).

- [ ] **Step 1: Write failing tests** in `test_load_finish.py` (editor user, a `customer`-like table made with `ops_helpers`/`mcp_helpers.applied`): `test_finish_makes_one_pending_change` (3 rows in two chunks; `change_id` present, change status `pending`, kind data, staging file gone); `test_finish_twice_returns_same_change` ; `test_two_threads_finish_once` (two concurrent calls via `ThreadPoolExecutor` with `transaction=True` marker; exactly one `Change` row for the script); `test_finish_after_cancel_refuses`; `test_empty_load_refuses`; `test_table_dropped_after_begin_refuses`; `test_bad_value_found_by_dry_run_reports_row_number_and_stays_open` (a text cell is fine for chunk checks but a unique-key duplicate fails the dry run; assert `row`, no `line`, load status `open`); `test_external_ref_orphan_reports_row` (child table `district` with `province_id` FK, parent `province` with unique `code`; load districts with `province_code` carrier; one unknown code; assert exactly one orphan with its load row number); `test_finish_in_non_master_database` (same flow in a created schema).
- [ ] **Step 2: Run** `pytest tests/test_load_finish.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement `finish` and move `trim_report`.**
- [ ] **Step 4: Run** the new tests, `tests/test_script_mcp.py` and `tests/test_script_submit.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(loads): finish_load through the script pipeline`.

### Task 6: Expiry purge

**Files:**
- Modify: `backend/scripts/loads.py`
- Test: `backend/tests/test_load_purge.py`

**Interfaces:**
- Produces: `purge_expired(now=None) -> int`: for every `open` load past `expires_at` (row-locked, `skip_locked`, re-checked under the lock) delete its file and set status `expired`; for every `cancelled` or `finished` load that still has a `path` delete the file and blank `path`. Returns the number of loads changed. Not scheduled here (the worker slice owns scheduling); tests call it directly.

- [ ] **Step 1: Write failing tests:** `test_open_expired_load_is_expired_and_file_deleted`; `test_open_fresh_load_is_kept`; `test_orphan_file_of_cancelled_load_is_removed`; `test_append_after_expiry_refuses`.
- [ ] **Step 2: Run** `pytest tests/test_load_purge.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement `purge_expired`** mirroring `scripts.storage.purge_expired`'s locking style.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(loads): purge expired staged loads`.

### Task 7: MCP and chat wiring, rules text

**Files:**
- Modify: `backend/mcp_server/tools.py`, `backend/mcp_server/app.py`, `backend/chat/toolspecs.py`, `backend/gate/operations.py` or wherever the "too many rows" refusal message for `insert_rows` is built (find with `grep -rn maxRowsPerDataOp backend --include=*.py`)
- Test: `backend/tests/test_mcp_load_tools.py`, extend `backend/tests/test_mcp_tools_registry.py` (role pin)

**Interfaces:**
- Consumes: `scripts.loads.begin/append/finish/get/cancel`, `LoadError`.
- Produces: in `tools.py` the functions `begin_load(user, table, columns, schema="master", refs=None)`, `append_rows(user, load_id, seq, csv)`, `finish_load(user, load_id, note="")`, `get_load(user, load_id)`, `cancel_load(user, load_id)` (each converts `LoadError`, `PermissionError`, `introspect.UnknownSchema` into `ToolFailure`), entries in `TOOL_ROLES` (all `"editor"`) and `IMPL`, texts in `DESCRIPTIONS` (the chunk-size advice of about 2,000 rows, the CSV rules, "never ask the person to run the insert themselves", "use these when a table needs more rows than maxRowsPerDataOp"), a `get_rules` section `"bulk_loading"` with a worked example, registration in `app.py` (`@mcp.tool` per tool, using `DESCRIPTIONS`) and the sentence in `INSTRUCTIONS`, the five specs in `chat/toolspecs.py`, and the row-cap refusal message ending with `Use begin_load, append_rows and finish_load for bigger data.`

- [ ] **Step 1: Write failing tests** in `test_mcp_load_tools.py`: `test_tools_exist_for_editor_and_not_viewer` (via `tools.allowed`); `test_full_flow_returns_change` (begin, two appends, finish through the `tools.*` functions); `test_bad_chunk_returns_problems_not_exception`; `test_misuse_raises_tool_failure` (wrong seq, unknown load); `test_get_rules_mentions_bulk_loading`; `test_row_cap_error_points_at_begin_load` (propose `insert_rows` with `maxRowsPerDataOp + 1` rows and assert the message mentions `begin_load`). Add the five tools with `"editor"` to the role pin in `test_mcp_tools_registry.py`.
- [ ] **Step 2: Run** `pytest tests/test_mcp_load_tools.py tests/test_mcp_tools_registry.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement the wiring.**
- [ ] **Step 4: Run** the two files plus `tests/test_mcp_http.py tests/test_mcp_chat_bridge.py tests/test_chat.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(mcp): begin_load, append_rows, finish_load, get_load, cancel_load`.

### Task 8: End to end, scale, docs

**Files:**
- Test: `backend/tests/test_mcp_load_e2e.py`
- Modify: `README.md` (MCP section: one short paragraph and the three-call example)

**Interfaces:** consumes the `tools.*` functions only.

- [ ] **Step 1: Write the tests:** `test_load_approve_and_rows_present` (editor loads 3,000 rows in 3 chunks; approver `approve_change`; `query` count equals 3,000; sample row values match, including a row with tab, backslash, newline, `\N` text and an empty-string text cell next to a NULL); `test_revert_removes_the_rows` (`revert_change` brings the count back); `test_unique_violation_found_at_finish_is_row_numbered`; `test_external_ref_load_end_to_end` (provinces then districts by `province_code`); `test_100k_rows` marked `@pytest.mark.slow` (register the marker in `pytest.ini` if missing): 20 chunks of 5,000, finish, approve, count; assert it finishes within 120 seconds.
- [ ] **Step 2: Run** `pytest tests/test_mcp_load_e2e.py -q`. Expected: FAIL on anything the earlier tasks left out, otherwise PASS; investigate any failure with systematic-debugging before changing code.
- [ ] **Step 3: Fix what the tests expose** (each fix gets its own failing test first if it is not already covered).
- [ ] **Step 4: Update `README.md`**, then run the whole backend suite (`pytest -q`, run it in the background and poll; it takes about five minutes). Expected: all pass.
- [ ] **Step 5: Commit** `test(loads): end to end and scale; docs`.
