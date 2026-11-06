# Script Intake, Database View and ERD — Design Spec

Date: 2026-11-06 · Status: draft for review · Builds on `2026-11-05-singularity-design.md` (milestones 1–7 are done).

## 0. Summary

Three additions to Singularity, built in this order:

- **A. Script intake.** A user uploads a `.sql` file. The AI validates it, then submits it as one ordinary change. The AI never carries the data itself, only an id. Raw script text is never executed.
- **B. Database view and export.** A Database tab (and MCP tools) to browse everything inside the database, and an export that a Postgres admin can run on another server. The export file is also a valid input for part A (round trip).
- **C. ERD.** A full-detail, good-looking entity-relationship diagram with export to image and text formats.

Why: loading 7,720 subdistrict rows through `propose_change` meant writing about 650 KB through tool arguments, in three stages because child rows need the generated ids of their parents. The tool is meant to be moved to another database once the design is good, so getting data and structure out cleanly matters as much as getting it in.

### Decisions made with the user

1. Script safety is **strict**: every statement must map to gate operations. Anything the gate cannot express is rejected with a reason. Same rules, same approval flow as today.
2. The AI validates a script **before** it can be submitted, and the check must show that it is compatible with and works on our system (rules, naming, a real dry run).
3. The export must be runnable by a Postgres admin, and the project must be able to move to another database.
4. The ERD must show full detail and look good.

### Assumptions (change any of these)

| Item | Default |
|---|---|
| Script size cap | 50 MB (`limits.maxScriptBytes`), nginx upload limit raised for `/api/scripts` only |
| Rows per script | 200,000 (`limits.maxRowsPerScript`); bigger loads use Postgres tools directly |
| Small inline script via MCP | up to 20 KB (`limits.maxInlineScriptBytes`) |
| Uploaded script lifetime if never submitted | 7 days |
| Who can do what | viewer: structure view and structure export. editor and approver: upload, validate, submit, data export |
| Parser | `pglast` (the real PostgreSQL parser, `libpg_query`) |
| Target schema | always `master` |
| Export retention | 24 hours |

## 1. Cleanups before the new work (milestone 8.0)

These came up while loading the Thai master data and would confuse the new features.

- The `auditColumns` rule still lists the key column as `id`, but the live tables use `<table>_id`. Update the rule through `propose_config_change` and confirm `gate/conventions.py` resolves the primary-key role from the database for every table.
- Constraint and index names created before the snake_case change are still camelCase (`fk_district_provinceId`, `district_provinceId_idx`, `subdistrict_districtId_idx`, `fk_subdistrict_districtId`). Rename through `drop_constraint`/`drop_index` plus `add_foreign_key`/`add_index` with explicit snake_case names.
- Part B adds a conformance report, which will list anything like this automatically.

## 2. Part A — Script intake

### 2.1 Flow

```
upload file ──► store (sha256, size) ──► split blocks ──► parse (pglast) ──► translate to ops
                                                                                   │
        submit ◄── report ◄── dry run (rolled back) ◄── gate rules + state check ◄─┘
          │
          ▼
   Change (pending) ──► approval (same rules as today) ──► apply in one transaction
```

Every step before "submit" changes nothing in `master`. The dry run runs the generated SQL in a transaction that is always rolled back.

### 2.2 Storage and channels

New Django app `scripts` (tables in the `singularity` schema).

`Script`: `id`, `uploader`, `filename`, `sha256`, `size`, `path` (file on a volume next to the backups, outside any web root), `status` (`uploaded | invalid | validated | submitted | expired`), `report` (JSON), `mapping` (JSON, optional), `change` (FK, nullable), `created_at`, `expires_at`.

| Channel | How |
|---|---|
| Web | **Scripts** tab: drop a file, see the report, press Submit |
| REST | `POST /api/scripts` (multipart), `GET /api/scripts/<id>`, `POST /api/scripts/<id>/validate`, `POST /api/scripts/<id>/submit`. Accepts the same personal access token as MCP so a user can `curl` a large file |
| MCP | `validate_script(script_id? , text? , mapping?)`, `submit_script(script_id? , text? , mapping?, prompt?)`, `get_script(script_id)` |

MCP cannot receive files, so the big file reaches the server through the web tab or REST, and the AI works with its `script_id`. A small script the AI writes itself can be passed as `text` (up to the inline cap). Roles: validate and submit need editor or above. Approval follows the existing rule (schema changes: approvers; data changes: proposer or approver).

### 2.3 Parsing

New module `backend/scripts/parse.py`, a pure function: `parse(text, mapping) -> ParseResult(ops, ignored, rejected, row_blocks)`.

1. **Block splitter.** pg_dump style files contain `COPY t (cols) FROM stdin;` followed by tab-separated lines and `\.`. These blocks are not SQL, so the splitter cuts them out first (text format: tab separator, `\N` for NULL, backslash escapes) and hands the rest to `pglast`. psql meta lines such as `\restrict` and `\unrestrict` (present in the real `master-data1` dump) are recognised and ignored with a note.
2. **Statement allowlist** (each maps to an existing or new gate operation):

| Statement | Operation |
|---|---|
| `CREATE TABLE` (types in the allowlist, NOT NULL, DEFAULT in the allowlist, PRIMARY KEY, UNIQUE, CHECK, inline or table-level FOREIGN KEY) | `create_table` |
| `ALTER TABLE ... ADD COLUMN / RENAME TO / RENAME COLUMN / ALTER COLUMN TYPE / ADD CONSTRAINT` | `add_column`, `rename_table`, `rename_column`, `alter_column_type`, `add_unique`, `add_foreign_key`, `add_check` |
| `CREATE [UNIQUE] INDEX` | `add_index` |
| `COMMENT ON COLUMN` / `COMMENT ON TABLE` | `set_column_comment` / new `set_table_comment` |
| `INSERT ... VALUES`, `COPY ... FROM stdin` | `insert_rows` (small) or `bulk_insert` (large) |
| `UPDATE` with a simple `WHERE` | `update_rows` |
| `DROP TABLE / COLUMN / INDEX / CONSTRAINT` | the matching drop op, **only** when the line before is `-- reason: <text>` |

3. **Ignored with a note** (harmless boilerplate): `SET ...`, `SELECT pg_catalog.set_config('search_path' ...)`, `ALTER ... OWNER TO`, `CREATE SCHEMA` for the one schema in the file, TOC comments, `SELECT setval(pg_get_serial_sequence(...))` (identity counters are fixed automatically after a bulk load, see 2.5).
4. **Rejected** with line, excerpt, reason and a hint: functions, procedures, triggers, views, `GRANT`/`REVOKE`, `CREATE EXTENSION`, `DELETE`, `TRUNCATE`, any statement touching a schema other than the file's single schema, any type or default outside the allowlist.
5. **Schema remap.** The file may use any single schema name (the real dump uses `master_thach`); every qualified name is remapped to `master`. Two or more schemas in one file are rejected.
6. **Type mapping.** `character varying(n)` → `varchar(n)`, `timestamp with time zone` → `timestamptz`, `int4` → `integer`, and so on; unmapped types are rejected.

### 2.4 Mapping (loading into tables whose shape differs from the file)

The user's dump uses natural keys (`district.district_id` is `'1001'`, `district.province_id` is `'10'`), while our tables have a generated key plus a business key (`code`). A small optional **mapping** JSON, which the AI can write in a few lines, tells the loader how to translate. The big data stays in the file.

```json
{
  "tables": {
    "province":    { "from": "province",    "columns": { "code": "province_id", "name": "province_name" } },
    "district":    { "from": "district",    "columns": { "code": "district_id", "name": "district_name" },
                     "refs": { "province_id": { "parent": "province", "by": "code", "from": "province_id" } } },
    "subdistrict": { "from": "subdistrict", "columns": { "code": "subdistrict_id", "name": "subdistrict_name", "postcode": "postcode" },
                     "refs": { "district_id": { "parent": "district", "by": "code", "from": "district_id" } } }
  }
}
```

`columns` maps target column to source column. `refs` resolves a foreign key from the parent's business key to the parent's generated key during the load. Tables are loaded in foreign-key order automatically, so all three tables load in **one change**. An unmatched key is an error listing the first 20 offenders and the total.

### 2.5 New and extended gate operations

All changes are in `gate/operations.py` (shape), `gate/state.py` (meaning), `gate/sqlgen.py` (SQL), `schema/executor.py` (apply and undo), and the operation text in `chat/toolspecs.py` and the MCP instructions.

- `add_check {table, name?, expression}` — the expression is parsed with `pglast` and limited to column references, literals, comparison operators, `~`, `IN`, `IS [NOT] NULL`, `AND/OR/NOT`, and the functions `length`, `char_length`, `lower`, `upper`. The real dump has `chk_subdistrict_postcode`, which needs this. The introspector already reads check constraints.
- `set_table_comment {table, comment}` — today only `create_table` can set a table comment.
- `on_delete` on `add_foreign_key` and on a column's `references`: `restrict` (default, matches "master data is never deleted"), `cascade`, `set null`, `no action`.
- `bulk_insert {table, source: {script_id, block, sha256, rows}, columns?, mapping?}` — the rows are **not** in the operation JSON. The `Change` stores only the reference, row count and block checksum. Execution reads the stored file, verifies the checksum, streams rows with `COPY ... FROM STDIN` into a temporary table, then `INSERT ... SELECT` with joins for `refs`. After a load that supplied explicit identity values, the executor sets the identity counter to the maximum value, so later inserts cannot collide.
- Revert of a bulk insert records the inserted primary keys per table and emits `deactivate_rows` operations in chunks of `maxRowsPerDataOp` (reverts already may exceed `maxOpsPerProposal`).

### 2.6 Validation report

Returned by `validate_script`, the REST validate call, and shown in the tab. Machine-readable so the AI can fix a script and validate again.

```
{ scriptId, status: "ok" | "errors",
  statements: { total, mapped, ignored, rejected },
  rejected: [ { line, excerpt, reason, hint } ],
  ignored:  [ { line, why } ],
  operations: { create_table: 3, bulk_insert: 3, ... },
  tables: [ { name, action, rows } ],
  violations: [ ...gate rule results, same shape as today... ],
  mappingErrors: [ ... ],
  dryRun: { ok, error?, seconds, skipped? },
  next: "text telling the AI what to do" }
```

- **Dry run.** The generated SQL runs through the same executor path on the read/write role inside one transaction, with `lock_timeout = 5s` and `statement_timeout = limits.dryRunTimeoutMs`, and is then rolled back. It catches duplicate keys, bad foreign keys, check violations and type errors with the first offending row. If it hits the timeout the report says `skipped` and adds a warning; the script can still be submitted.
- **Compatibility** means: parse OK, gate rules pass (naming, types, audit columns, business key and so on, evaluated on the translated operations), schema state check passes (tables and columns exist or are created earlier), and the dry run succeeds.
- Validation results are cached with the schema fingerprint and rules hash. Submitting re-checks, exactly as `propose` does today, and a moved schema marks the change `stale`.

### 2.7 Submit and approval

`submit_script` calls a new `approvals.service.propose_script`, which creates an ordinary `Change` (kind `schema` or `data`, source `script`). It stores compact operations, the report as `check_result`, a diff made of counts (for example `~ bulk insert 7720 rows into subdistrict`), and a short `generated_sql` preview (first statements plus "and 7,720 rows via COPY"). The approval popup and queue show the script name, size, checksum and a download link. The script file is kept while the change is pending or applied and removed with the retention job afterwards.

### 2.8 Security

- Script text is parsed, never executed. Executed SQL is only what `sqlgen` generates from validated operations; identifiers go through `sql.Identifier`; values travel through the COPY protocol or literals as today.
- UTF-8 only, NUL bytes rejected, size and row caps enforced while streaming, files stored outside the web root, checksum verified at apply time.
- Validate has a per-user rate limit (the dry run uses locks). Events `script.uploaded`, `script.validated`, `script.submitted` go to the audit log.
- The gate evaluates every translated operation, including those inside `bulk_insert` targets, so a script cannot bypass a rule an AI proposal must follow.

### 2.9 Testing

Golden fixtures: a trimmed copy of the real `master-data1` dump (a few rows per table) with its mapping. Unit tests per allowed and per rejected statement, the COPY block reader (NULLs, tabs, escapes, Thai text), schema remap, the type map, check-expression limits. Integration tests with the existing `tests/pgsetup.py`: dry run leaves no trace, apply equals the operation path, revert removes bulk rows, an unmatched `refs` key is reported, a tampered file fails the checksum.

## 3. Part B — Database view and export

### 3.1 Database view

New REST and MCP read tools (viewer):

- `GET /api/database` and MCP `get_database_overview`: schema `master` only (the internal `singularity` schema is never shown); per table: comment, row estimate, exact count on request, size on disk, columns (type, nullability, default, identity, comment), constraints (including CHECK text), indexes, relations in and out, and flags.
- `GET /api/database/tables/<name>`: the above plus sample rows (reuses `schema/rows.py`) and column statistics (reuses the engine stats).
- `GET /api/database/conformance` and MCP `check_conformance`: runs today's gate rules against the **live** schema and lists what does not comply (for example camelCase names, missing audit columns). This is how drift like the camelCase leftovers is found automatically.
- Flags: no primary key, no table or column comment, foreign key column without an index, never-used or duplicate index, nullable foreign key, share of inactive rows.

The **Database** tab: a tree on the left (schema → table → columns, constraints, indexes) with search; on the right, tabs for Columns, Constraints, Indexes, Relations, Sample rows, DDL (the generated `CREATE TABLE`) and Stats; totals and flag badges across the top.

### 3.2 Export

`POST /api/exports` and MCP `export_database`, then `GET /api/exports/<id>` (status) and `GET /api/exports/<id>/download` (a signed link that expires). The MCP tool returns the id, link, size and checksum, never the file content.

Options: `scope` (whole schema or chosen tables), `include` (`structure | data | both`), `format` (`sql | zip`), `dataFormat` (`copy | insert`), `targetSchema` (default `master`), `includeInactive` (default true, full fidelity).

**Generator.** Our own writer built from the catalog (`backend/exports/sqlwriter.py`), not `pg_dump` output. Reason: the export has to be valid for a plain `psql` run and also acceptable to the part A parser, and `pg_dump` adds owners, `SET` lines and psql meta commands. The whole export reads in one `REPEATABLE READ, READ ONLY` transaction on the read-only role, so structure and data are one consistent snapshot.

SQL file layout:

1. Header comment: tool version, time, source Postgres version, schema fingerprint, per-table row counts, content checksum.
2. `BEGIN;` and `CREATE SCHEMA IF NOT EXISTS <target>;`
3. `CREATE TABLE` in dependency order, with primary key, unique and check constraints inline; types from the allowlist only, so they are portable; table and column `COMMENT ON` lines.
4. Data as `COPY ... FROM stdin` blocks (or `INSERT` when asked), parents before children.
5. Foreign keys and indexes after the data (faster, and order-independent).
6. Identity counters: `SELECT setval(pg_get_serial_sequence(...), max, true);` (the part A parser recognises this exact shape and treats it as already handled).
7. `COMMIT;`

No owners, grants, roles or extensions. Run with `psql -v ON_ERROR_STOP=1 -f export.sql`.

Zip layout: `schema.sql`, `data/<table>.csv` (UTF-8, header row, `\N` for NULL), `load.sql` (`\copy ... WITH (FORMAT csv, HEADER true, NULL '\N')` commands for the CSV files), `manifest.json` (version, fingerprint, per table row count and sha256, rules snapshot, who and when), `verify.sql` (row-count checks to run after loading), `erd.dbml`, `erd.mmd`, `README.txt` (the three steps for an admin).

Permissions and limits: structure export is viewer; data export is editor and above; every export is audited (`export.created`, `export.downloaded`). Files are streamed to an export volume, reachable only through the signed link, and removed after `exports.retentionHours`. Large exports run in a background thread in the api container for now (the `worker` container stays a placeholder until needed).

### 3.3 Testing

Round trip in CI, using `tests/pgsetup.py`:

1. Seed a schema and data, export to SQL.
2. Load into a fresh Postgres 16 with `psql`; compare the schema fingerprint and a per-table row hash.
3. Validate and submit the same export through part A into an empty `master`; compare again.
4. Same for the zip variant. Also unit tests for dependency order (including a cycle), quoting of odd identifiers, Thai text, NULL versus empty string, and timestamptz.

## 4. Part C — ERD

### 4.1 Data

Extend `introspect.graph()` additively (the current 3D view and its tests keep working). Per column: `default`, `identity`, `unique`, `comment`, and `fk {table, column, constraint, onDelete}`. Per table: check constraints and indexes. Per relation: `constraint`, `onDelete`, `optional` (the foreign key column is nullable) and `cardinality` (`one-to-many`, or `one-to-one` when the foreign key column is unique).

### 4.2 Renderer

New `frontend/components/Erd.tsx`, in SVG (crisp zoom, clean export, printable). It replaces `SchemaDiagram` in the 2D mode of the Schema tab; the 3D universe stays.

- **Table cards:** header with name, group colour and row estimate; one row per column with badges **PK**, **FK**, **UQ**, **NN**, **IDX**; type on the right, default muted, comment on hover. Three density modes: names only, keys only, all columns.
- **Relations:** crow's-foot markers (one / many, optional / mandatory), anchored at the foreign-key column row and the primary-key row, routed with right angles; constraint name and `ON DELETE` shown on hover or selection.
- **Navigation:** search for a table or column; select a table to highlight it and its neighbours up to N hops and dim the rest; filter by group; zoom and pan with fit-to-screen and a minimap; keyboard reachable. A legend explains the symbols.
- **Look:** dark black-hole theme by default, a light print theme for sheets and PDFs.
- **Groups:** derived from a shared name prefix (three or more tables), overridable per user in local storage.

### 4.3 Layout

Layered layout with crossing reduction: layers from foreign-key depth (the idea already in `frontend/lib/layout.ts`), ordering by barycenter sweeps, ports aligned to column rows, orthogonal routing with channel allocation. Implemented in the Rust engine (`engine/src/layout.rs`), exposed as WASM in the browser and PyO3 in the backend, with the TypeScript twin required to pass the same golden cases in `engine/testdata/` (the pattern the repo already uses). Hand-moved card positions are saved in local storage per user; a Reset layout button restores the computed one.

### 4.4 Export

From the ERD page: **SVG** (standalone, styles embedded), **PNG** (2x), **PDF** (print stylesheet, A3 landscape through the browser print dialog, so no extra dependency). Text formats **Mermaid** (`erDiagram`) and **DBML** are produced on the server (`backend/exports/erd_text.py`) from the same graph, are included in the zip export, and are also available as MCP `get_erd(format)`.

### 4.5 Testing

Layout golden cases (layering, crossings, ports); component tests for badges, edge count, selection and filter; accessibility checks (names, focus order); a performance budget on a synthetic 60-table schema (layout under 500 ms, first render under 1 s); a screenshot check of the real Thai master data schema.

## 5. Settings added

`singularity.yaml` and `settings_app/tunables.py`: `limits.maxScriptBytes`, `limits.maxInlineScriptBytes`, `limits.maxRowsPerScript`, `limits.dryRunTimeoutMs`, `scripts.retentionDays`, `exports.retentionHours`. All editable by approvers through the existing settings flow.

## 6. Milestones

| # | Milestone | Contents |
|---|---|---|
| 8.0 | Cleanups | `auditColumns` rule, constraint and index renames |
| 8 | Script intake | 8a new operations (`add_check`, `on_delete`, `bulk_insert`) and the parser; 8b store, validate, REST and MCP; 8c bulk executor, submit, approval UI, Scripts tab |
| 9 | Database view | overview API and MCP tools, conformance report, Database tab |
| 10 | Export | SQL writer, zip bundle, signed download, round-trip tests with part A |
| 11 | ERD | graph extension, Rust layout, `Erd.tsx`, image and text exports |

Milestone 11 depends only on the graph extension, so it can run in parallel with 8 if wanted. Milestone 10's round-trip test needs 8.

## 7. Out of scope

`DELETE` and `TRUNCATE` in scripts; views, functions, triggers, partitions, other schemas; incremental sync or diff between two databases; non-Postgres targets; loads above `maxRowsPerScript` (use Postgres tools, the export format is plain SQL for that); encrypted exports; shared real-time ERD editing.

## 8. Risks

- **Dry run takes locks on `master`.** Bounded by `lock_timeout`, the statement timeout and a per-user rate limit; scripts that cannot finish in time still validate everything else.
- **`pglast` is a C extension.** Wheels exist for common platforms; the Docker image builds it, and a failed build is caught in CI before release.
- **Mapping mistakes.** A wrong mapping can load valid but misplaced data. The report shows per table row counts and the first rows after translation, and the approver sees the same report.
- **Large approval diffs.** Replaced by counts and a downloadable script, so reviewers are not asked to read thousands of lines.
