# Plan D: Database Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Schema tab with a Database tab: pick or create a database (Postgres schema), then manage its tables and fields, relations, ERD, search and SQL export, all through the gated change flow.

**Architecture:** Pure helpers build structured operations (`lib/databaseOps.ts`); one shared `ProposeDialog` reviews them, posts them with the selected schema (`proposeChange(..., schema)`) and shows the resulting `ChangeCard`. `DatabaseView` (rail + table pane with sub-tabs Fields / Relations / ERD / Overview) owns the selected schema and table. No new write endpoints; one small backend addition (`hiddenColumns` on table detail) so audit columns are never hard-coded in the UI.

**Tech Stack:** Next.js 15, React 19, TypeScript, Vitest + Testing Library (existing style: `vi.mock("@/lib/api")`, dynamic import of the component).

**Spec:** `docs/superpowers/specs/2026-11-09-database-tab-design.md` section 4 (and 1.2 for audit-column wording). Plan C (backend) is merged: `GET /api/schema/schemas` returns `{"schemas":[{name,tables,comment}]}`; `?schema=` on tables/graph/detail/rows/options/query/export/search; absent means `master`; bad schema is a 400 `{"error"}`; `propose` body takes `schema`; schema ops (`create_schema`, `rename_schema`, `drop_schema`) must be alone in their change and only approvers decide them.

## Global Constraints

- Tab id `database` replaces `schema` in `AppShell`; label "Database".
- Roles: viewers read only (no write buttons rendered at all); editors propose; approvers approve (existing `canDecide` in `lib/changes.ts`, unchanged).
- Names typed in the UI: `^[a-z][a-z0-9_]{0,62}$` (snake_case, 63 chars); schema names additionally refuse `singularity`, `public`, `information_schema` and anything starting `pg_`, and names that already exist. The gate stays the authority; the UI only prevents obvious mistakes.
- Column types offered: text, integer, bigint, smallint, boolean, date, timestamptz, timestamp, uuid, jsonb, real, double precision, numeric, numeric(p,s), varchar(n). Defaults: JSON literal or one of `now()`, `gen_random_uuid()`, `current_date`.
- Destructive ops (`drop_column`, `drop_table`, `drop_schema`, `drop_index`, `drop_constraint`) need a non-empty `reason`.
- `create_table` never includes audit columns (the server adds them). Audit columns are hidden in Fields by default, shown read-only with the **Show audit columns** toggle; the toggle is stored per browser in `localStorage` inside try/catch.
- Audit column names always come from the server (`hiddenColumns`), never from a list in the UI.
- Reuse existing CSS classes (`grid-bar`, `button`, `link`, `muted`, `small`, `problems`, `change`, `schema-scroll`, `universe-wrap`); add only the few layout classes the rail needs to `frontend/app/globals.css`.
- Commits authored as `VynnLockwood <120536136+VynnLockwood@users.noreply.github.com>`; NO Claude co-author, "Generated with Claude Code" or Claude-Session lines.
- Frontend checks: `cd frontend && npx vitest run && npx tsc --noEmit` (baseline: 157 tests pass). Backend check for Task 1: `pg_ctlcluster 16 main start; . /tmp/testenv.sh; cd backend && pytest -q` (baseline: exactly 21 failures: 18 in `tests/test_duplicates.py` plus `test_config_changes.py::test_stale_schema_proposals_are_marked_when_rules_change`, `test_mcp_http.py::test_full_flow_ai_proposes_user_confirms_then_applies`, `test_script_submit.py::test_editor_submit_data_only_then_self_approve_loads_rows`; compare by name).

## Review Focus

1. Switching database while a table is selected or a form is open: the selection resets and no request goes out with the old table and the new schema (or the reverse).
2. A viewer never gets a write button, including in empty states (no schemas beyond master, no tables).
3. A proposal the server refuses (400/422 with `{"error"}` or gate violations) keeps the dialog open with the message and the typed input intact.
4. Names that are uppercase, contain spaces, are reserved, duplicate an existing name or exceed 63 characters show an inline error and disable submit.
5. `localStorage` throwing (private mode) never breaks the page; audit columns can never be edited even when shown.
6. A failed export download (403 for a viewer asking for data, 422 for an unsupported column type) shows the server's message instead of a silent no-op.
7. Loading and error states for the schema list, graph and table detail; a database with zero tables.

---

## File Structure

- Modify `backend/schema/views.py` (table detail adds `hiddenColumns`), test in `backend/tests/test_api_schemas.py`.
- Modify `frontend/lib/api.ts` (schema-aware calls and types), `frontend/lib/api.test.ts`.
- Create `frontend/lib/storage.ts` (`usePersistentFlag`).
- Create `frontend/lib/databaseOps.ts` + `databaseOps.test.ts` (validators, op builders, `describeOp`).
- Create `frontend/components/ProposeDialog.tsx` (+ test).
- Create `frontend/components/DatabaseView.tsx` (shell, rail, header), `ExportMenu.tsx`, `NewDatabaseForm.tsx`, `FieldsTab.tsx`, `NewTableForm.tsx`, `RelationsTab.tsx`, `OverviewTab.tsx` (+ a test file each).
- Modify `frontend/components/SchemaView.tsx` (becomes the ERD: `schema` prop, no export controls), `SearchBox.tsx` (`schema` prop), `AppShell.tsx`, `AppShell.test.tsx`, `SchemaView.test.tsx`, `frontend/app/globals.css`.

---

### Task 1: Backend: audit column names on table detail

**Files:**
- Modify: `backend/schema/views.py` (`table_detail`)
- Test: `backend/tests/test_api_schemas.py`

**Interfaces:**
- Produces: `GET /api/schema/tables/<name>?schema=` adds `"hiddenColumns": [str]`, sorted: the table's columns whose names are in `roles.hidden_columns()` plus the column `roles.roles_of(d, conv)` assigns to any audit role including `primary_key` (find the exact helper use in `schema/rows.py:60-70` and `duplicates/service.py:55-60`).

- [ ] **Step 1: Write failing tests** in `test_api_schemas.py`: for a table created through the gate (audit columns added automatically), `hiddenColumns` contains `created_at`, `updated_at`, `created_by`, `is_active` and the primary key column, and not `name`/`code`; same for a `crm` table with `?schema=crm`; a table with no audit columns returns `[]`.
- [ ] **Step 2: Run, expect FAIL:** `pytest backend/tests/test_api_schemas.py -k hidden -v`.
- [ ] **Step 3: Implement** in `table_detail`: after `describe_table`, add the key. Compute from conventions, never from literal names.
- [ ] **Step 4: Run the file and the full backend suite; failures must equal the 21 baseline names.**
- [ ] **Step 5: Commit** `feat(api): table detail lists audit columns`.

---

### Task 2: Frontend API client and persistent flag

**Files:**
- Modify: `frontend/lib/api.ts`, `frontend/lib/api.test.ts`
- Create: `frontend/lib/storage.ts`, `frontend/lib/storage.test.ts`

**Interfaces (Produces, used by all later tasks):**
```ts
export interface SchemaInfo { name: string; tables: number; comment: string | null }
export const getSchemas: () => Promise<SchemaInfo[]>            // unwraps {"schemas": [...]}
export const getGraph: (schema?: string) => Promise<SchemaGraph> // default "master"
export interface TableDetailColumn { name: string; type: string; nullable: boolean; default: string | null; comment: string | null; identity: boolean }
export interface TableDetail { name: string; comment: string | null; columns: TableDetailColumn[];
  constraints: { name: string; kind: string; definition: string }[];
  indexes: { name: string; definition: string }[]; hiddenColumns: string[] }
export const getTable: (name: string, schema?: string) => Promise<TableDetail>
export const getRows: (table: string, q: RowsQuery, schema?: string) => Promise<RowsPage>
export const getOptions: (table: string, schema?: string) => Promise<RefOptions>
export const proposeChange: (operations: Operation[], prompt: string, source?: "grid"|"web", schema?: string) => Promise<Change>
export const searchSchema: (q: string, schema?: string) => Promise<SearchResult>
// Change gains `schema?: string`; SearchHit gains `schema?: string`.
// exportUrl(schema, data) already exists.
// frontend/lib/storage.ts
export function usePersistentFlag(key: string, initial: boolean): [boolean, (v: boolean) => void]
```
Schema defaults to `"master"` and, for master, URLs stay exactly as today (no `schema=` param) so existing callers and tests do not change; for other schemas append `schema=<encoded>` (use `?` or `&` correctly).

- [ ] **Step 1: Write failing tests**: `getSchemas` unwraps; `getGraph()` hits `/schema/graph`, `getGraph("crm")` hits `/schema/graph?schema=crm`; `getTable("t","crm")`; `getRows` with filters and schema builds one query string; `proposeChange(ops,"p","web","crm")` body has `schema: "crm"`, and omits `schema` for master; `searchSchema("x","crm")`. `usePersistentFlag`: reads stored `"1"`, writes on set, survives `localStorage.getItem`/`setItem` throwing (returns initial, set still updates state).
- [ ] **Step 2: Run, expect FAIL:** `cd frontend && npx vitest run lib/api.test.ts lib/storage.test.ts`.
- [ ] **Step 3: Implement** the signatures above.
- [ ] **Step 4: Run the whole frontend suite and `npx tsc --noEmit`; 157 existing tests still pass.**
- [ ] **Step 5: Commit** `feat(frontend): schema-aware API client`.

---

### Task 3: Operation builders and validators

**Files:**
- Create: `frontend/lib/databaseOps.ts`, `frontend/lib/databaseOps.test.ts`

**Interfaces (Produces):**
```ts
export const COLUMN_TYPES: string[]            // the list in Global Constraints (numeric/varchar entries offered as "numeric(p,s)" / "varchar(n)" templates)
export function validateName(name: string, taken: string[]): string | null          // null = ok, else message
export function validateSchemaName(name: string, taken: string[]): string | null    // adds the reserved-name rules
export function validateType(t: string): string | null
export function parseDefault(input: string): unknown   // "" -> undefined; now()/gen_random_uuid()/current_date -> the string; true/false/null/numbers -> JSON value; anything else -> the string
export interface ColumnSpec { name: string; type: string; nullable?: boolean; unique?: boolean; default?: string;
  comment?: string; references?: { table: string; column?: string } }
export const createSchemaOp:  (name: string, comment?: string) => Operation
export const renameSchemaOp:  (schema: string, newName: string) => Operation
export const dropSchemaOp:    (schema: string, reason: string) => Operation
export const createTableOp:   (name: string, columns: ColumnSpec[], comment?: string) => Operation
export const addColumnOp:     (table: string, column: ColumnSpec) => Operation
export const renameTableOp:   (table: string, newName: string) => Operation
export const renameColumnOp:  (table: string, column: string, newName: string) => Operation
export const alterColumnTypeOp: (table: string, column: string, type: string) => Operation
export const setColumnCommentOp: (table: string, column: string, comment: string) => Operation
export const dropColumnOp:    (table: string, column: string, reason: string) => Operation
export const dropTableOp:     (table: string, reason: string) => Operation
export const addIndexOp:      (table: string, columns: string[], unique?: boolean) => Operation
export const addUniqueOp:     (table: string, columns: string[]) => Operation
export const addForeignKeyOp: (table: string, column: string, refTable: string, refColumn?: string, onDelete?: string) => Operation
export const dropIndexOp:     (name: string, reason: string) => Operation
export const dropConstraintOp:(table: string, name: string, reason: string) => Operation
export const isDestructive: (op: Operation) => boolean
export const describeOp: (op: Operation) => string      // one readable line, e.g. "Add column customer.phone (text)"
```
Op shapes are exactly those in `backend/chat/toolspecs.py` `_OPS` (e.g. `{op:"add_column", table, column:{...}}`, `{op:"drop_table", table, reason}`). Optional fields are omitted, not sent as `undefined`/empty.

- [ ] **Step 1: Write failing tests**: `validateName`: accepts `customer`, `a1_b`; refuses `""`, `Customer`, `has space`, `1abc`, 64 chars, a taken name (message names the problem); `validateSchemaName` also refuses `public`, `singularity`, `information_schema`, `pg_x`; `validateType` accepts every `COLUMN_TYPES` entry plus `numeric(10,2)`, `varchar(40)`, refuses `varchar`, `varchar(0)`, `serial`, `text; drop`; `parseDefault` table (`""`→undefined, `now()`, `5`→5, `true`→true, `abc`→"abc"); each builder returns the exact object (assert with `toEqual`, including omission of empty optionals); `isDestructive` true only for the five drop ops; `describeOp` snapshot-free assertions on the lines for create_table/add_column/drop_column/create_schema/drop_schema.
- [ ] **Step 2: Run, expect FAIL:** `npx vitest run lib/databaseOps.test.ts`.
- [ ] **Step 3: Implement** the module (no React, no fetch).
- [ ] **Step 4: Run, expect PASS, plus `npx tsc --noEmit`.**
- [ ] **Step 5: Commit** `feat(frontend): database operation builders`.

---

### Task 4: ProposeDialog

**Files:**
- Create: `frontend/components/ProposeDialog.tsx`, `ProposeDialog.test.tsx`

**Interfaces:**
- Consumes: `proposeChange`, `ApiError`, `Change`, `Me` (api.ts); `describeOp`, `isDestructive` (Task 3); `ChangeCard`.
- Produces:
```tsx
export function ProposeDialog(props: {
  me: Me; schema: string; title: string;
  build: (reason: string) => Operation[];   // called with the typed reason ("" when not destructive)
  destructive?: boolean;                      // shows a required Reason input
  onClose: () => void;
  onProposed: (change: Change) => void;
}): JSX.Element
```
Behaviour: first step lists `describeOp` lines of `build(reason)` and, when `destructive`, a required "Reason" field (button disabled until non-empty); **Propose** calls `proposeChange(ops, title, "web", schema)`, then shows the returned change in a `ChangeCard` (approvers see Approve/Reject through the shell's popup, editors "Waiting for an approver") and calls `onProposed`. On `ApiError` it stays on the first step, keeps the reason, shows `error.message` in a `problems` paragraph (role alert). A change that comes back with check errors/blockers is shown via `ChangeCard` as-is. Escape and a Close button call `onClose`.

- [ ] **Step 1: Write failing tests**: lists operation lines; destructive requires reason and passes it to `build`; Propose sends `(ops, title, "web", "crm")` and renders the change card; refused proposal shows the message and keeps the reason text and the Propose button; `onProposed` called once; Close and Escape call `onClose`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(frontend): propose dialog`.

---

### Task 5: DatabaseView shell, rail, AppShell swap, ERD

**Files:**
- Create: `frontend/components/DatabaseView.tsx`, `DatabaseView.test.tsx`
- Modify: `frontend/components/SchemaView.tsx`, `SchemaView.test.tsx`, `AppShell.tsx`, `AppShell.test.tsx`, `frontend/app/globals.css`

**Interfaces:**
- Consumes: `getSchemas`, `getGraph` (Task 2).
- Produces:
```tsx
export type DbTab = "fields" | "relations" | "erd" | "overview";
export function DatabaseView(props: {
  me: Me; live: Change[]; refreshKey: number;
  onProposed: (c: Change) => void; onAskAssistant: (text: string) => void;
}): JSX.Element
// SchemaView becomes the ERD: ({ schema, refreshKey }: { schema: string; refreshKey: number }) — fetches getGraph(schema); the export controls move out (Task 6).
```
Rail: schema list with table counts (selected one highlighted, `aria-current`), a "New database" button slot (rendered only when `me.role !== "viewer"`; wired in Task 7), then the tables of the selected schema from `getGraph(schema)` with a filter input. Main: table header and a tablist with Fields / Relations / ERD / Overview (Fields, Relations and Overview render a placeholder `<p className="muted">` in this task; Tasks 8, 10, 11 fill them). With no table selected, ERD and Overview still work (whole database). Selecting another schema clears the selected table and aborts stale responses (ignore results from earlier requests). `AppShell`: tab union `"database"` replaces `"schema"`, label "Database", renders `<DatabaseView ... refreshKey={schemaKey} />`; the old `SearchBox` + `SchemaView` block moves into DatabaseView/Overview (Task 11).

- [ ] **Step 1: Write failing tests** (mock `@/lib/api`): rail lists `master (3)` and `crm (0)` from `getSchemas`; clicking `crm` loads its graph with `getGraph("crm")` and shows "No tables yet" for an empty one; viewer sees no "New database" button, editor does; switching schema while a table is selected clears the table header (Review Focus 1); load failure shows `problems` text; AppShell shows a "Database" tab and no "Schema" tab, and `refreshKey` changes refetch; ERD renders `SchemaDiagram` for the selected schema (adapt the existing SchemaView tests: drop the export assertions, add `schema` prop).
- [ ] **Step 2: Run, expect FAIL:** `npx vitest run components/DatabaseView.test.tsx components/SchemaView.test.tsx components/AppShell.test.tsx`.
- [ ] **Step 3: Implement** the shell, the SchemaView change, AppShell wiring, and the rail/layout CSS (`.db-layout` two columns, one column under 800px like `.universe-wrap`).
- [ ] **Step 4: Run the full frontend suite and `tsc`; expect PASS.**
- [ ] **Step 5: Commit** `feat(frontend): Database tab shell replaces Schema tab`.

---

### Task 6: Export menu (with visible failures)

**Files:**
- Create: `frontend/components/ExportMenu.tsx`, `ExportMenu.test.tsx`
- Modify: `DatabaseView.tsx` (header)

**Interfaces:**
- Consumes: `exportUrl` (existing), `Me`.
- Produces: `ExportMenu({ schema, canExportData }: { schema: string; canExportData: boolean })` rendering the "Include data" checkbox (disabled with a title for viewers, as today) and a **Download SQL** button. On click it does a preflight `fetch(url)` with an `AbortController`; as soon as headers arrive: `!ok` → show the server's `{"error"}` (or status text) in a `problems` role alert and do not download; ok → abort the preflight body and start the download by navigating an `<a href download>` to the same URL. Viewers never build `data=1` URLs.

- [ ] **Step 1: Write failing tests** (mock `fetch`): ok response triggers a download click with `.../schema/export?schema=crm&data=0`; checkbox on (editor) uses `data=1`; viewer's checkbox is disabled and URL stays `data=0`; a 422 `{"error":"column foo has type vector…"}` shows that message and starts no download; a 403 shows its message; network failure shows "Could not reach the server."
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** and mount it in the `DatabaseView` header next to the schema name.
- [ ] **Step 4: Run, expect PASS; full suite + `tsc`.**
- [ ] **Step 5: Commit** `feat(frontend): export menu shows failures`.

---

### Task 7: New, rename and delete database

**Files:**
- Create: `frontend/components/NewDatabaseForm.tsx`, `NewDatabaseForm.test.tsx`
- Modify: `DatabaseView.tsx` (+ its test)

**Interfaces:**
- Consumes: `ProposeDialog`, `createSchemaOp`, `renameSchemaOp`, `dropSchemaOp`, `validateSchemaName`.
- Produces: `NewDatabaseForm({ me, existing, onClose, onProposed })` (name + optional comment, live inline validation, submit disabled while invalid, opens `ProposeDialog` with `createSchemaOp`). Header actions in `DatabaseView` (hidden for viewers and for `master`): **Rename** (small form + `renameSchemaOp`) and **Delete** (disabled with a title "Only empty databases can be deleted" while the schema has tables; else `ProposeDialog` with `destructive` and `dropSchemaOp`). After a proposal, if the change is `applied` or later an `applied` event bumps `refreshKey`, the schema list refetches; if the schema just created appears, select it; if the selected schema vanished (renamed/deleted), fall back to `master`.

- [ ] **Step 1: Write failing tests**: invalid names (Review Focus 4: `Sales`, `my db`, `public`, `pg_x`, existing `crm`, 64 chars) show inline errors and disable submit; valid name opens the dialog whose Propose sends exactly `[{op:"create_schema",name:"sales"}]` with schema `master`; Rename/Delete absent for viewers and for `master`; Delete disabled for a non-empty schema and sends `drop_schema` with the typed reason for an empty one; after `refreshKey` bump the new database is selected; deleted selected schema falls back to master.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS; full suite + `tsc`.**
- [ ] **Step 5: Commit** `feat(frontend): create, rename and delete databases`.

---

### Task 8: Fields tab

**Files:**
- Create: `frontend/components/FieldsTab.tsx`, `FieldsTab.test.tsx`
- Modify: `DatabaseView.tsx` (mount, plus table rename/drop header actions)

**Interfaces:**
- Consumes: `getTable`, `usePersistentFlag("db.showAudit", false)`, `ProposeDialog`, builders from Task 3.
- Produces: `FieldsTab({ me, schema, table, refreshKey, onProposed })`. Table of columns (name, type, null?, default, comment); audit columns (names in `detail.hiddenColumns`) hidden until **Show audit columns** is ticked, then shown read-only with no action buttons. For non-viewers each editable column has **Rename**, **Type**, **Comment**, **Drop** (destructive, reason), and the tab has **Add field** (name, type select from `COLUMN_TYPES`, nullable, unique, default via `parseDefault`, comment) → `addColumnOp`. Table header actions in `DatabaseView` for non-viewers: **Rename table**, **Drop table** (destructive).

- [ ] **Step 1: Write failing tests**: audit columns hidden by default, shown (without buttons) when toggled, toggle persisted (`localStorage` key) and surviving a throwing `localStorage`; Add field sends `[{op:"add_column",table:"customer",column:{name:"phone",type:"text"}}]` with the selected schema; nullable/unique/default/comment omitted when untouched; invalid field name or type blocks submit; Drop requires a reason and sends `drop_column`; Rename/Type/Comment send the matching ops; Drop/Rename table send `drop_table`/`rename_table`; viewers see the table but no buttons; refetch on `refreshKey`; load error and empty-columns states.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS; full suite + `tsc`.**
- [ ] **Step 5: Commit** `feat(frontend): fields tab`.

---

### Task 9: New table

**Files:**
- Create: `frontend/components/NewTableForm.tsx`, `NewTableForm.test.tsx`
- Modify: `DatabaseView.tsx` (rail button "New table", non-viewers)

**Interfaces:**
- Consumes: `createTableOp`, `validateName`, `validateType`, `parseDefault`, `ProposeDialog`, `getGraph` result for the reference picker.
- Produces: `NewTableForm({ me, schema, tables, onClose, onProposed })`: table name, optional comment, a list of column rows (name, type, nullable, unique, default, comment, optional reference to a table in the same database, with column defaulting to its primary key), add/remove row, at least one column required, no duplicate column names, no audit-column inputs (note "audit columns are added automatically"). Submit opens `ProposeDialog` with `createTableOp`.

- [ ] **Step 1: Write failing tests**: refuses empty column list, duplicate column names, invalid table/column names and types (inline errors, submit disabled); a valid form sends the exact `create_table` op with `references: {table}` only when chosen; reference picker lists only tables of the selected database; viewers have no "New table" button.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS; full suite + `tsc`.**
- [ ] **Step 5: Commit** `feat(frontend): create tables`.

---

### Task 10: Relations tab

**Files:**
- Create: `frontend/components/RelationsTab.tsx`, `RelationsTab.test.tsx`
- Modify: `DatabaseView.tsx` (mount)

**Interfaces:**
- Consumes: `getTable` (constraints, indexes), `getGraph` relations, builders.
- Produces: `RelationsTab({ me, schema, table, graph, refreshKey, onProposed })`: lists "Points to" and "Referenced by" from the graph, constraints (primary key / unique / check / foreign key) and indexes from table detail. Non-viewers get **Add index** (columns multi-select, unique checkbox), **Add unique**, **Add foreign key** (column, reference table from the same database, optional reference column, on delete select from `restrict | cascade | set null | no action`), and **Drop** on each non-primary-key constraint and index (destructive, reason).

- [ ] **Step 1: Write failing tests**: lists both directions; Add index/unique/FK send the exact ops (`add_index {table,columns,unique?}`, `add_unique`, `add_foreign_key {table,column,ref_table,ref_column?,on_delete?}`); Drop constraint sends `drop_constraint {table,name,reason}` and drop index `drop_index {name,reason}` only with a reason; primary key has no Drop; viewers see no buttons; empty-state text.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS; full suite + `tsc`.**
- [ ] **Step 5: Commit** `feat(frontend): relations tab`.

---

### Task 11: Overview tab (search and assistant)

**Files:**
- Create: `frontend/components/OverviewTab.tsx`, `OverviewTab.test.tsx`
- Modify: `SearchBox.tsx`, `SearchBox.test.tsx`, `DatabaseView.tsx`

**Interfaces:**
- Produces: `SearchBox` gains optional `schema?: string` passed to `searchSchema(q, schema)`; hits show their schema when present. `OverviewTab({ me, schema, graph, onAskAssistant })`: schema name, comment, counts of tables/columns, the existing `SearchBox` scoped to this database (`canPropose = me.role !== "viewer"`, `canReindex = me.role === "approver"`), and an "Ask the assistant" button that calls `onAskAssistant` with a prompt naming the database (the shell already opens Chat with that draft).

- [ ] **Step 1: Write failing tests**: search calls `searchSchema("q","crm")`; hits from another schema show that schema; "Ask the assistant" text mentions the selected database; reindex button only for approvers; an empty database shows the counts as 0 without errors.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS; full suite + `tsc`.**
- [ ] **Step 5: Commit** `feat(frontend): database overview tab`.

---

### Task 12: Integration pass

**Files:**
- Create: `frontend/components/DatabaseFlow.test.tsx`
- Modify: whatever the tests expose

- [ ] **Step 1: Write the integration test** (mocked API, real components): approver opens Database, creates `sales` (proposal asserted), schema list refreshes via `refreshKey` and selects it, creates a table `customer` with two columns in `sales` (asserted op and `schema` sent), opens Fields (audit columns hidden, toggled on), adds a field, a viewer user sees the same screens with zero write buttons, the export menu shows a server error.
- [ ] **Step 2: Run; fix real defects in the owning component with a focused test each.**
- [ ] **Step 3: Run `npx vitest run`, `npx tsc --noEmit`, and `npx next build` (no type or lint errors). Report counts.**
- [ ] **Step 4: Commit** `test(frontend): database tab flow`.

---

## Self-Review

- Spec coverage: 4.1 layout (rail Task 5/7/9, main + sub-tabs Tasks 5, 8, 10, 11, header actions Tasks 6, 7, 8); 4.2 managing (new database Task 7, tables Tasks 8/9, fields Task 8, indexes/unique/FK Task 10, audit toggle Task 8, empty/loading/error states every task); 4.3 backend (schemas endpoint, `?schema=` done in Plan C; `hiddenColumns` Task 1; no new write endpoints); Plan D tests: rail, form validation, audit hiding, add-field op, viewer buttons, export menu, Overview (Tasks 5 to 12). Plan B deferred note (failed export display) Task 6.
- Decisions the spec left open (flag to the user): a review-and-reason step before the change is sent (`ProposeDialog`); the export preflight request; Data and Duplicates tabs stay on `master` (not in the spec).
- Types: all later tasks use only the names defined in Task 2 and Task 3 interfaces; `Operation` is the existing `Record<string, unknown>`.
- Proportion: 12 tasks, no component bodies in the plan.
