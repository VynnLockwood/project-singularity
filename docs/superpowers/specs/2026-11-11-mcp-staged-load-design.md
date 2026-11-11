# MCP staged load: big data through chat, gated

Date: 2026-11-11. Status: draft for review.

## 1. Problem and goal

A data change holds at most `limits.maxRowsPerDataOp` (1,000) rows per operation and `limits.maxOpsPerProposal` (200) operations, and the AI must write every row out as text. For 10k to 100k rows the AI gives up and hands the "insert data" part back to the person. The only large-data path today is script intake (`bulk_insert`), reachable only by uploading a file in the web app.

**Goal.** An AI client with no shell and no file access (Claude web or mobile) can create the schema and then load 10k to 100k rows into an existing table by itself, over MCP. The load is gated, validated, approved once, applied in one transaction after a backup, and undoable like any other change. Small data keeps using `propose_change`.

**Success.** An editor's AI loads 100,000 rows in about 40 chunk calls, sees per-row errors for any bad chunk and fixes only that chunk, finishes with one pending change, and after approval the rows are in the table. A dropped session can continue the load.

**Not in this slice.** Update or upsert mode, loading from a URL or file, JSON row format, loading several tables in one change, any web-app UI for loads.

## 2. Design in one picture

```
begin_load  ->  append_rows (x N, any order of retries)  ->  finish_load
 (table,cols)     (csv text, seq number)                      builds a COPY script,
 returns load_id  validated per chunk, stored on disk         runs the existing validate + submit
                                                              -> one Change (bulk_insert)
```

The existing script pipeline does everything after `finish_load`: gate rules, foreign-key checks, a rolled-back dry run, one `bulk_insert` operation (no per-op row cap, `expected_rows` checked), approval, backup, revert. A staged load only has to produce a stored `Script` whose text is a single `COPY` block for the target table, plus an identity mapping.

## 3. Tools (editor and approver only; viewers do not get them)

### `begin_load(table, columns, schema="master")`
- The table must exist in a managed schema; `columns` are existing, distinct, non-generated column names in the order the CSV will use.
- Every NOT NULL column without a default (other than the key and audit columns the server fills) must be listed; otherwise the call fails at once and names the missing columns.
- Returns `{load_id, columns, next_seq: 1, limits: {chunk_bytes, chunk_rows, total_rows, total_bytes, expires_at}}`.
- At most `loads.maxOpenPerUser` (default 5) open loads per user.

### `append_rows(load_id, seq, csv)`
- `csv` is header-less RFC 4180 text, columns in the `begin_load` order. An unquoted empty field is NULL; a quoted empty field `""` is the empty string.
- `seq` must equal `next_seq`. Resending the last accepted `seq` with identical content succeeds and changes nothing (safe retry). Any other `seq` fails and says which one is expected.
- Checks per chunk: well-formed CSV, field count, NOT NULL, and a cell check for common types (integer, numeric, boolean, date, timestamp, uuid, enum values). Unknown types pass and are settled by the dry run in `finish_load`.
- A chunk with any bad row is rejected as a whole and nothing is stored. The error lists up to 20 problems as `{row, column, value, message}` with `row` counted from the first row of the whole load, so the AI resends only that chunk with the same `seq`.
- Success returns `{rows_in_chunk, total_rows, next_seq}`.
- Limits: `limits.maxLoadChunkBytes` (default 262,144), `limits.maxLoadChunkRows` (default 5,000), total rows `limits.maxScriptRows` (already enforced by the gate, reused), total bytes `limits.maxLoadBytes` (default 104,857,600).

### `finish_load(load_id, note="")`
- Builds the COPY script from the stored rows, saves it as a `Script`, validates it against the table with an identity mapping, and when it validates, submits it. Returns the same shape as `submit_script`: `{load_id, script_id, report, change_id, next}`.
- Validation errors are returned with the row number (not the script line), the load stays open, and the AI can append or cancel. Rows already stored cannot be edited, so a row-level problem found here means cancel and reload, or fix the data in a follow-up change.
- Calling it again after success returns the same `change_id`.
- After success the load is `finished` and refuses appends.

### `get_load(load_id)` and `cancel_load(load_id)`
- `get_load`: status, table, columns, `next_seq`, `total_rows`, `total_bytes`, `expires_at`, `change_id`.
- `cancel_load`: deletes the staged file and marks the load cancelled. Only the owner or an approver.
- An owner sees only their own loads; approvers see all.

`get_rules` gains a short "Loading more than N rows" section with a worked example, the CSV conventions, the chunk size advice (about 2,000 rows), and the server instructions text in `mcp_server/app.py` says: use these tools when a table needs more rows than `maxRowsPerDataOp`; never ask the person to run the insert themselves. The row-cap error from `propose_change` points at `begin_load`.

## 4. Storage

New model `StagedLoad` (app `scripts`): `owner`, `schema`, `table`, `columns` (JSON), `status` (`open`, `finished`, `cancelled`, `expired`), `next_seq`, `rows`, `bytes`, `last_chunk_sha`, `script` (FK, null until finished), `created_at`, `expires_at`.

Rows are kept in one append-only file under `SCRIPT_DIR/loads/` (private directory, random name), already in COPY text form: `\` becomes `\\`, tab `\t`, newline `\n`, carriage return `\r`, NULL `\N`. The file is not the source of truth for its own length: `StagedLoad.bytes` is. An append writes and fsyncs the chunk, then commits the new `rows`, `bytes`, `next_seq` and `last_chunk_sha` in one transaction holding a row lock on the load. On every read the file is cut back to `bytes`, so a crash between the write and the commit leaves no half chunk.

`finish_load` writes `COPY <schema>.<table> (<columns>) FROM stdin;`, the stored body, and `\.` into a new script file through the existing `storage` module and registers the `Script`.

Expiry: an open load expires `loads.ttlHours` (default 24) after its last append. The existing script purge also deletes expired and cancelled load files.

## 5. Approval, audit, undo

- The resulting change goes through `approvals.propose(..., source="script")` exactly like a submitted script, so whatever approval rules apply to a script change today apply here. This slice does not change them.
- `begin_load`, `finish_load` and `cancel_load` write audit events. `append_rows` does not (volume); the change records the row count.
- Undo uses the reverse operations `bulk_insert` already returns (delete by generated key). A table with no primary key cannot be undone automatically, same as scripts today.

## 6. Security and robustness

- The AI's CSV is data, never SQL: the server converts it to COPY text and the executor loads it through a temp table of text columns, as it does for scripts.
- Per-user caps on open loads and total staged bytes; a chunk over the limits is refused before parsing.
- Names are checked against the live table; `schema` goes through `introspect.require_managed`.
- A load can only target the schema and table it began with; `finish_load` re-checks that the table and columns still exist and refuses with a clear message if they changed.

## 7. Settings

New tunables in `settings_app/tunables.py` (shown in the Settings view with the others): `limits.maxLoadChunkBytes`, `limits.maxLoadChunkRows`, `limits.maxLoadBytes`, `loads.ttlHours`, `loads.maxOpenPerUser`.

## 8. Testing

- Unit: CSV to COPY conversion (tab, backslash, newline, quotes, NULL vs empty string, unicode); cell checks per type; chunk limits.
- `append_rows`: next seq, retry with same content (no change), retry with different content (error), skipped seq (error), bad chunk stores nothing, crash simulation (file longer than `bytes` is cut back).
- Permissions: viewer has no tools; user cannot see another user's load.
- End to end over the MCP layer: begin, append 3 chunks, finish, change pending, approve, rows present and counted, revert removes them; a load where `finish_load` reports a foreign-key problem; expiry purge.
- A 100,000-row load completes within a generous time bound (marked slow).

## 9. Open question to settle during planning

Child tables that reference a parent by a generated id. `bulk_insert` already supports `refs` (match a parent business key, fill the generated id), driven by the script mapping. Planning must check whether the mapping can name a parent that already exists in the target schema and is not in the script. If it can, `begin_load` gains an optional `refs` argument (`column`, `parent_table`, `parent_key`, `source_column`) so an AI can load districts keyed by province code. If it cannot, planning adds the smallest extension to the mapping to allow it, and says so in the plan.
