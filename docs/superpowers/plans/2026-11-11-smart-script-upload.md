# Smart Script Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person can drop a pg_dump of a few hundred MB on the Scripts tab and, without further clicks, get "Ready: N tables, M rows" (or plain-language problems), with unsupported-but-harmless statements skipped and reported.

**Architecture:** The parser sorts statements into imported / skipped-with-reason / error. Uploads arrive as chunks written to disk and are streamed into normal script storage. Validation runs in a separate process the API starts, writing phase and heartbeat into `Script.report`; the page polls `GET /api/scripts/<id>`. The page chains upload, validate and result by itself and keeps mapping under "Customize".

**Tech Stack:** Django 5, pglast, pytest (Postgres), Next.js, vitest + Testing Library, nginx.

**Spec:** `docs/superpowers/specs/2026-11-11-smart-script-upload-design.md`

**Test environment:** `pg_ctlcluster 16 main start`, `. /tmp/spc.sh`, `cd backend && pytest <path> -q`. Script tests use the fixture pattern of `tests/test_load_finish.py` (`script_dir` autouse with `invalidate_overrides`, `pytestmark = [django_db, usefixtures("master")]`). Frontend: `cd frontend && npx vitest run <file>` and `npx tsc --noEmit`.

## Global Constraints

- Skipped statements are never executed and never change what is imported; only `ScriptIssue`-level classification changes. DROP, DELETE, UPDATE, TRUNCATE, unknown statements and syntax errors stay errors.
- Classification uses the pglast statement type, not text matching.
- `limits.maxScriptBytes` default 536870912 (512 MiB), tunable range up to 2147483648; chunk size 8388608 (8 MiB); upload sessions expire after 24 hours; a running validation with no heartbeat for 10 minutes counts as failed.
- Upload and validate endpoints are editor-only with the existing CSRF rules; a script is visible to its owner and approvers only.
- The single-request `POST /api/scripts` (file or JSON paste), `submit_script`, `validate_script` and the synchronous validate path keep working unchanged for small scripts.
- A script assembled from chunks has exactly the same stored bytes and sha256 as the same file sent in one request (BOM stripped, CRLF and lone CR to LF, UTF-8 and NUL checks).
- Plain-language problem text is shown first; the raw parser message stays available under a "Details" fold.

## Review Focus

1. A multi-byte UTF-8 character, or a CRLF pair, split across two chunks assembles to the same bytes and sha256 as a single upload; a resent chunk, out-of-order chunks and a short last chunk are handled. (Task 3)
2. A skipped `CREATE FUNCTION` with `$$ ... ; ... $$` is skipped whole; a DROP, DELETE or UPDATE after skipped statements still errors; a script of only skipped statements reports "Nothing to import". (Task 2)
3. A killed or never-started validation process turns into a failed state instead of "running" forever; two validations of one script do not run at once; another user cannot start, poll or complete someone else's job or upload. (Tasks 3, 4)
4. A 250 MB, 192,000-row, 80-table dump goes through chunked upload and background validation inside a fixed memory bound. (Tasks 1, 7)
5. After a failed upload the page still shows the previous script with Validate enabled; auto-validate runs once per upload and customizing the mapping re-runs it without looping. (Task 5)

---

### Task 1: Scale harness and memory trimming

**Files:**
- Create: `backend/tests/bigdump.py`, `backend/tests/test_script_scale.py`
- Modify: `backend/scripts/storage.py` (`read`), `backend/scripts/sqlparse.py` (`parse_script`), `backend/scripts/validate.py` (`_run`)

**Interfaces:**
- Produces: `bigdump.write_dump(path, tables=80, rows=192_000, target_bytes=250*2**20) -> dict` (writes a pg_dump 18 style file: `\restrict` header, SET lines, CREATE TABLE per table with a text key and Thai text columns, COPY blocks, trailing PK constraints; returns `{bytes, tables, rows}`); `bigdump.PROBE` (the small mixed sample from the spec: extension, sequence, grant, function, trigger, `nextval` default, COPY); Task 2 and Task 7 import both.

- [ ] **Step 1: Write `bigdump.py`** and the slow test `test_250mb_dump_validates_within_memory_bound`: save the generated dump with `storage.save` as an editor, run `validate.validate(script, None, "master")`, assert `report["ok"]`, `report["tables"]` has 80 entries and rows sum to 192,000, and peak resident memory (`resource.getrusage(RUSAGE_SELF).ru_maxrss` measured in a subprocess running the same call, so earlier tests do not skew it) is below `MEM_LIMIT_MB`. First set `MEM_LIMIT_MB = None`.
- [ ] **Step 2: Run** `pytest tests/test_script_scale.py -q -s -m slow`. Expected: PASS and prints seconds and peak MB; record both in the ledger. If it errors (timeout, parse limit), that is the finding: fix it before continuing.
- [ ] **Step 3: Remove avoidable copies** in `storage.read` (return bytes once, no second normalization), `parse_script` (skip `replace("\r\n", "\n")` and the BOM slice when absent; storage already normalizes) and `validate._run` (drop the `bytes` reference before parsing). Re-run step 2 and record the new peak.
- [ ] **Step 3b: Set `MEM_LIMIT_MB`** to the measured peak rounded up by 25% and make the test assert it. Run it again. Expected: PASS.
- [ ] **Step 4: Run** `pytest tests/test_script_*.py tests/test_sqlparse*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `perf(scripts): scale harness and fewer copies on read/parse`.

### Task 2: Tolerant parser

**Files:**
- Modify: `backend/scripts/sqlparse.py` (`ParsedScript.skipped`, `_dispatch`, `create_table` and `alter_table` default handling), `backend/scripts/validate.py` (`report["skipped"]`, `skipped_truncated`, "Nothing to import" error)
- Test: `backend/tests/test_sqlparse_skip.py`, `backend/tests/test_script_validate_skipped.py`

**Interfaces:**
- Consumes: `bigdump.PROBE`.
- Produces: `ParsedScript.skipped: list[dict]` of `{line, statement, reason, kind}` with `kind` in `"extension" | "sequence" | "default" | "permission" | "logic"` (`"logic"` for function, procedure, trigger, rule, policy); `report["skipped"]` (same dicts, capped at `MAX_IGNORED`, rest counted in `report["skipped_truncated"]`); a parsed script with no create/load work and only skipped or ignored statements adds a parse error `"Nothing to import: the file has no tables or rows."`.

- [ ] **Step 1: Write failing tests** in `test_sqlparse_skip.py`: `test_probe_parses_with_no_errors` (PROBE yields no errors, one `create_table`, one row stream); one parametrized `test_skip_kinds` case per statement class (`CREATE EXTENSION`, `COMMENT ON EXTENSION`, `CREATE SEQUENCE`, `ALTER SEQUENCE .. OWNED BY`, `SELECT pg_catalog.setval(..)` already ignored stays ignored, `GRANT`, `REVOKE`, `ALTER DEFAULT PRIVILEGES`, `CREATE FUNCTION` with a `$$` body containing `;`, `CREATE TRIGGER`, `CREATE POLICY`) asserting `skipped[0]["kind"]` and `reason`; `test_nextval_default_dropped` (inside CREATE TABLE and via `ALTER TABLE ONLY .. ALTER COLUMN .. SET DEFAULT nextval(..)`: no error, the column has no `default`, one `skipped` entry of kind `"default"` per column); `test_destructive_statements_still_error` (DROP TABLE, DELETE, UPDATE, TRUNCATE, `ALTER TABLE .. DROP COLUMN` each yield an error after a skipped statement); `test_function_body_skipped_whole` (the statement after the function still parses). In `test_script_validate_skipped.py`: `test_report_lists_skipped_and_is_ok` (PROBE as an uploaded script validates `ok` with `skipped` populated, dry run passed) and `test_only_skipped_is_nothing_to_import`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** in `_dispatch` a branch per pglast statement type (`CreateExtensionStmt`, `CommentStmt` on `OBJECT_EXTENSION`, `CreateSeqStmt`, `AlterSeqStmt`, `GrantStmt`, `GrantRoleStmt`, `AlterDefaultPrivilegesStmt`, `CreateFunctionStmt`, `CreateTrigStmt`, `RuleStmt`, `CreatePolicyStmt`) that appends to `out.skipped` and returns without error; in `_map_default` treat a `nextval(...)` call as "drop default" (return a sentinel the callers turn into no default plus a `skipped` entry). Copy `skipped` into the report in `validate._run` next to `_ignored`.
- [ ] **Step 4: Run** the new tests plus `pytest tests/test_sqlparse*.py tests/test_script_*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(scripts): skip and report extensions, sequences, grants and logic instead of failing`.

### Task 3: Chunked upload backend

**Files:**
- Create: `backend/scripts/uploads.py`; migration under `backend/scripts/migrations/` (generated)
- Modify: `backend/scripts/models.py` (`UploadSession`), `backend/scripts/storage.py` (`save_stream`, `uploads_dir`, purge helper), `backend/scripts/api.py` (four views), `backend/config/urls.py`, `backend/config/siconfig.py` and `backend/settings_app/tunables.py` (`maxScriptBytes` default and range), `nginx/nginx.conf`
- Test: `backend/tests/test_script_chunked_upload.py`

**Interfaces:**
- Produces:
  - Model `UploadSession(owner, name, size, chunk_bytes, received JSON list[int], path, status {open,complete,expired}, script FK null, created_at, expires_at)`.
  - `uploads.begin(user, name: str, size: int) -> dict` returns `{upload_id, chunk_bytes, chunks, max_bytes}`; refuses `size` over `limits.maxScriptBytes` or `size <= 0` with a `storage.ScriptError` whose text names both numbers.
  - `uploads.put_chunk(user, upload_id: int, n: int, data: bytes) -> dict` writes at offset `n * chunk_bytes` (`n` in range; length equals `chunk_bytes` except the last chunk, which must equal the remainder), records `n` in `received`, returns `{received: [...]}`; a resend of the same `n` overwrites.
  - `uploads.status(user, upload_id) -> {received, size, chunks}`; `uploads.complete(user, upload_id) -> Script` (every chunk present, else `ScriptError` naming missing ones; calls `storage.save_stream`; deletes the part file; sets status `complete`).
  - `storage.save_stream(owner, name, src_path: str) -> Script`: one pass over the file with an incremental UTF-8 decoder, strips a leading BOM, refuses NUL and invalid UTF-8, converts CRLF and lone CR to LF (including a pair split at a read boundary), hashes and writes the normalized bytes to the script file, and returns the same `Script` row shape as `save`.
  - Views: `POST /api/scripts/uploads`, `PUT /api/scripts/uploads/<id>/<n>`, `GET /api/scripts/uploads/<id>`, `POST /api/scripts/uploads/<id>/complete`; an expired or completed session answers 410; another user's session answers 404.
  - `uploads.purge_expired(now=None) -> int` (parts and rows of expired sessions); scheduling stays with the worker slice.

- [ ] **Step 1: Write failing tests:** `test_chunks_equal_single_upload` (a 30 KB text with a Thai character straddling a chunk boundary and a CRLF pair straddling another, `chunk_bytes` patched to 4096 with a monkeypatched constant; sha256, size and stored bytes equal `storage.save` of the same text); `test_bom_and_lone_cr_match_single_path`; `test_out_of_order_and_resent_chunk`; `test_short_last_chunk_and_wrong_length_refused`; `test_missing_chunk_named_at_complete`; `test_over_cap_refused_at_begin_with_numbers`; `test_nul_and_invalid_utf8_refused_at_complete`; `test_other_users_session_is_404_and_viewer_403`; `test_expired_session_is_410_and_purged`; `test_http_flow` (Django test client: begin, put, get, complete return the shapes above and a `201` with `script_id`).
- [ ] **Step 2: Run** `pytest tests/test_script_chunked_upload.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the model, migration (`python manage.py makemigrations scripts`), `save_stream` (never holds more than one read block plus the decoder tail), `uploads.py`, views and URLs. Raise `limits.maxScriptBytes` default to 536870912 and its tunable max to 2147483648; add `location /api/scripts/uploads { client_max_body_size 16m; ... }` before the existing `/api/scripts` block.
- [ ] **Step 4: Run** the new tests plus `pytest tests/test_script_storage.py tests/test_script_settings.py tests/test_settings.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(scripts): chunked upload streamed to disk`.

### Task 4: Background validation job

**Files:**
- Create: `backend/scripts/jobs.py`, `backend/scripts/management/commands/validate_script.py` (and `__init__.py` files)
- Modify: `backend/scripts/validate.py` (`progress` callback), `backend/scripts/service.py`, `backend/scripts/api.py` (`validate_view` `?async=1`, `_summary` stale detection)
- Test: `backend/tests/test_script_jobs.py`

**Interfaces:**
- Consumes: Task 1's trimmed `validate`.
- Produces:
  - `validate.validate(script, mapping, schema="master", progress=None)`: calls `progress(phase: str)` with `"reading"`, `"parsing"`, `"checking"`, `"dry_run"` in order.
  - `jobs.start(user, script_id, mapping, schema) -> dict`: refuses a submitted or expired script and a script already running (`ScriptError`, 409); writes `report = {"state": "running", "phase": "queued", "heartbeat": <iso>, "job": {"mapping": ..., "schema": ...}}`; starts `[sys.executable, "manage.py", "validate_script", str(id)]` with `subprocess.Popen(..., start_new_session=True, cwd=BASE_DIR)`; returns `{"state": "running"}`.
  - Command `validate_script <id>`: reads `job` from the report, runs `validate` with a `progress` that updates `phase` and `heartbeat` (single `UPDATE`), and on any exception stores `{"state": "failed", "message": ...}`; on finish the stored report is the normal report plus `"state": "done"`.
  - `API: POST /api/scripts/<id>/validate?async=1` returns 202 `{"state": "running"}`; `_summary` turns a `running` report whose heartbeat is older than 600 seconds into `{"state": "failed", "message": "The check stopped unexpectedly. Try again."}` (without writing it).

- [ ] **Step 1: Write failing tests:** `test_progress_phases_in_order`; `test_job_runs_to_done_and_report_equals_sync` (monkeypatch `jobs.subprocess.Popen` to call the command function inline; the stored report minus `state`, `phase`, `heartbeat`, `job` equals a synchronous validate of the same script); `test_second_start_refused_while_running`; `test_start_refused_for_submitted_script`; `test_failed_job_stores_message` (patch `validate` to raise); `test_stale_running_report_reads_as_failed` (heartbeat 11 minutes old); `test_killed_process_never_started` (Popen patched to do nothing; after the heartbeat ages 11 minutes the summary says failed and a new `start` is accepted); `test_other_user_gets_403_or_404_for_validate_and_detail`; `test_async_view_returns_202`.
- [ ] **Step 2: Run** `pytest tests/test_script_jobs.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `progress` hooks at the four phase boundaries in `validate._run`, `jobs.py`, the command, and the view and summary changes. `start` treats a stale running report as not running.
- [ ] **Step 4: Run** the new tests plus `pytest tests/test_script_*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(scripts): validation as a background job with progress`.

### Task 5: One-click Scripts page

**Files:**
- Modify: `frontend/lib/api.ts`, `frontend/components/ScriptsTab.tsx`, `frontend/app/legacy.css` (small additions only)
- Create: `frontend/lib/scriptProblems.ts`
- Test: `frontend/components/ScriptsTab.test.tsx` (extend), `frontend/lib/scriptProblems.test.ts`

**Interfaces:**
- Produces in `api.ts`: `uploadScriptChunked(file: File, onProgress: (done: number, total: number) => void, signal?: AbortSignal) => Promise<{script_id, sha256, size}>` (begin, put chunks in order with three retries per chunk and `GET` to resume, complete); `validateScriptAsync(id, mapping?) => Promise<{state: "running"}>`; `getScript(id)` unchanged; `ScriptReport` gains `state?: "running" | "done" | "failed"`, `phase?: string`, `message?: string`, `skipped?: {line, statement, reason, kind}[]`, `skipped_truncated?: number`.
- Produces `scriptProblems.explain(err: {kind, line, statement, message}) => {text: string; fix: string}` mapping the known parser and mapping messages to a sentence and a fix, with a generic fallback that keeps the raw message in `fix`.
- Page behaviour: choosing a file runs `uploadScriptChunked`, then `validateScriptAsync` with the current customize mapping (none by default), then polls `getScript` every 2000 ms until `state` is `done` or `failed`; phases render as one line ("Uploading 63%", "Reading", "Parsing", "Checking rows", "Dry run", "Ready"); the size limit comes from a `GET /api/scripts/limits` read of `maxScriptBytes` shown before a file is chosen and checked client-side before sending; result block "Ready: N tables, M rows" with Submit; collapsed "Not imported, and why" grouped by `kind` (a `logic` group is expanded and marked as a warning) and "Notes" for `ignored`; problems via `explain` with a "Details" fold; "Customize (optional)" fold holds the destination picker and the mapping JSON and re-runs validation once when it changes; a failed upload shows its reason and "Try again" and keeps the previous script and an enabled Validate.

- [ ] **Step 1: Add** `GET /api/scripts/limits` (backend, one view returning `{max_bytes, chunk_bytes}`) with a test `test_limits_endpoint` in `backend/tests/test_script_chunked_upload.py`; run it red then green.
- [ ] **Step 2: Write failing tests** in `ScriptsTab.test.tsx` (mock `@/lib/api` with `vi.resetModules()` + `vi.doMock`, `fireEvent`): `choosing a file uploads then validates then shows Ready without another click`; `validate runs once per upload`; `a failed upload keeps the previous script and Validate stays enabled` and shows the reason and a Try again button; `skipped statements render under Not imported and a logic group is open`; `file over the limit is refused before any request`; `changing a customize field re-runs validation once`; `a failed job shows the message and a Try again`. In `scriptProblems.test.ts`: one case per known message class plus the fallback.
- [ ] **Step 3: Run** `npx vitest run components/ScriptsTab.test.tsx lib/scriptProblems.test.ts`. Expected: FAIL.
- [ ] **Step 4: Implement** the api functions, `scriptProblems.ts` and the page changes. Keep the existing destination-picker tests passing (the picker moves under the fold but keeps its labels).
- [ ] **Step 5: Run** `npx vitest run` and `npx tsc --noEmit`. Expected: all pass.
- [ ] **Step 6: Commit** `feat(web): one-click script upload with progress, skipped list and plain problems`.

### Task 6: MCP and docs alignment

**Files:**
- Modify: `backend/mcp_server/tools.py` (`submit_script`, `get_script` text only), `README.md`, `backend/chat/toolspecs.py` (none expected)
- Test: `backend/tests/test_script_mcp.py` (extend)

**Interfaces:**
- Consumes: Task 2 `report["skipped"]`.

- [ ] **Step 1: Write failing test** `test_submit_script_with_extension_and_grant_succeeds_and_reports_skipped` (inline PROBE through `tools.submit_script`: `change_id` present and `report["skipped"]` non-empty).
- [ ] **Step 2: Run** it. Expected: FAIL only if the trimmed report drops `skipped`; keep `_trim_report` passing the key through and cap it at 20.
- [ ] **Step 3: Update** the `submit_script` description sentence on what is skipped, and the README Scripts section (limit, chunked upload, "Not imported", Customize, the `?async=1` option for API users).
- [ ] **Step 4: Run** `pytest tests/test_script_mcp.py tests/test_mcp_tools_registry.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `docs(scripts): skipped statements in MCP and README`.

### Task 7: End to end and scale

**Files:**
- Test: `backend/tests/test_script_upload_e2e.py`

**Interfaces:** consumes Tasks 1 to 4 only.

- [ ] **Step 1: Write the tests:** `test_chunked_upload_validate_submit_approve_roundtrip` (PROBE through the HTTP views: begin, chunks of 1 KB, complete, `validate?async=1` with Popen patched inline, poll `GET` to `done`, submit, approve as approver, province row present); `test_250mb_chunked_end_to_end` marked `slow`: `bigdump.write_dump`, upload in 8 MiB chunks through the views, `jobs` run in a real subprocess (Popen not patched; poll up to 20 minutes), result `done` and `ok`, 80 tables, 192,000 rows, and the child process peak memory below Task 1's `MEM_LIMIT_MB`.
- [ ] **Step 2: Run** `pytest tests/test_script_upload_e2e.py -q`. Expected: PASS; fix any failure they expose with a failing test first.
- [ ] **Step 3: Run the whole backend suite** (background, poll; about 7 minutes) and `cd frontend && npx vitest run && npx tsc --noEmit`. Expected: all pass.
- [ ] **Step 4: Commit** `test(scripts): upload end to end and scale`.
