# UI rework 2c: the Database tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Database tab opens by link (`/database/<db>/<table>?tab=`), its Fields, Relations and Overview tabs use the new design parts, and the ERD lets you select a table, see its relations highlighted, and open its fields.

**Architecture:** Frontend only. Selected database and table come from the address (`route` + `navigate`, as in the Data tab); the tab is a query value read and written with `history.replaceState`, held per `db/table` key so a stale tab is never used (the 2b lesson). A shared `TableList` is extracted from `TablePicker`. All proposal logic (`ProposeDialog`, `lib/databaseOps`) is untouched.

**Tech Stack:** Next.js 15 / React 19, vitest + Testing Library (`fireEvent`; no user-event in the repo). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-11-ui-database-design.md`

## Global Constraints

- Address: `/database` replaces itself with `/database/master` (or the first database if `master` is gone); `/database/<db>` is that database's Overview; `/database/<db>/<table>` is Fields; `?tab=fields|relations|erd|overview`, unknown or impossible values (Fields/Relations with no table) mean the default.
- Unknown database or table: inline message "There is no database X." / "There is no table X in Y."; lists stay usable; no `getTable` call for an unknown table.
- A table or database that vanishes after a reload falls back (replace) to the database Overview / `master`.
- Choosing a database keeps the ERD tab if it is the current tab, otherwise lands on Overview (existing behaviour).
- Nothing about what is proposed changes: same operations, titles, review step. Viewers see no actions, menus or forms.
- Under 800px the left pane is a "Databases and tables" disclosure; choosing something closes it.
- Every existing test keeps passing except the ones this plan lists as changed (locators that moved with the markup, and the test helpers that now supply `route` and `navigate`).
- Commit messages end with the two trailer lines the session attribution reminder gives.

## Review Focus

1. A hand-edited address (`/database/ghost`, `/database/master/nope`, `?tab=bogus`, `?tab=fields` with no table) shows a message or the default tab, never a blank page, and never calls `getTable` for a table that is not in the list. (Task 3)
2. Switching table or database never shows or sends the old table's detail, and an old slow response never overwrites the current one. (Tasks 3, 4)
3. The created-database auto-select still happens only when the user is idle and never after they navigated elsewhere. (Task 3, existing tests carried over)
4. Viewers never get a menu, form or button that proposes anything; the row menu closes on Escape and outside click and returns focus to its trigger. (Tasks 4, 5)
5. Table names with spaces, quotes or markup (`a b`, `x"y`, `<b>`) render as text in the list, the ERD and the panel, and "Open fields" navigates to the correctly encoded path. (Task 7)

---

### Task 1: Tab address helpers

**Files:**
- Create: `frontend/lib/databaseRoute.ts`
- Test: `frontend/lib/databaseRoute.test.ts`

**Interfaces:**
- Produces: `type DbTab = "fields" | "relations" | "erd" | "overview"`; `parseTab(search: string, hasTable: boolean): DbTab`; `tabToSearch(tab: DbTab, hasTable: boolean): string` (returns `""` for the default tab, else `"?tab=<tab>"`). Default is `fields` with a table, `overview` without. `DatabaseView` re-exports `DbTab` from here.

- [ ] **Step 1: Write the failing tests:** `parseTab("?tab=relations", true) === "relations"`; `parseTab("", true) === "fields"`; `parseTab("", false) === "overview"`; `parseTab("?tab=bogus", true) === "fields"`; `parseTab("?tab=fields", false) === "overview"`; `parseTab("?tab=relations", false) === "overview"`; `parseTab("?tab=erd", false) === "erd"`; repeated `tab` keys take the first; `tabToSearch("fields", true) === ""`, `tabToSearch("overview", false) === ""`, `tabToSearch("erd", true) === "?tab=erd"`, `tabToSearch("overview", true) === "?tab=overview"`.
- [ ] **Step 2: Run** `cd frontend && npx vitest run lib/databaseRoute.test.ts`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** both functions with `URLSearchParams`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(database): tab address helpers`.

### Task 2: Shared `TableList`

**Files:**
- Create: `frontend/components/TableList.tsx`
- Modify: `frontend/components/TablePicker.tsx` (use it), `frontend/app/legacy.css` if a class moves
- Test: `frontend/components/TableList.test.tsx`; `TablePicker.test.tsx` must pass unchanged

**Interfaces:**
- Produces: `TableList(props: { tables: { name: string; rowEstimate?: number }[]; selected: string; onSelect(name: string): void; searchLabel: string; emptyText: string; noMatchText: string; estimates?: boolean; listLabel?: string })`. Renders the search box (`type="search"`, `aria-label={searchLabel}`, placeholder = `searchLabel`), then either `emptyText` (no tables), `noMatchText` (search matches none), or a `<ul className="table-list" aria-label={listLabel}>` of buttons (`aria-current="true"` on the selected one). With `estimates` false the button's accessible name is exactly the table name (no icon text, no count). Search is case-insensitive contains on a local state. It does not know about `<details>`; the caller closes its own disclosure in `onSelect`.

- [ ] **Step 1: Write the failing tests:** search narrows case-insensitively; selected has `aria-current`; `emptyText` and `noMatchText` appear in their cases; with `estimates={false}` `getByRole("button", { name: "customer" })` matches exactly even when `rowEstimate` is given; with `estimates` the count is shown; clicking calls `onSelect(name)`; a name like `<b>x</b>` renders as text.
- [ ] **Step 2: Run** `cd frontend && npx vitest run components/TableList.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement `TableList`** and change `TablePicker` to render it (search label "Search tables", `emptyText` "No tables in this database.", `noMatchText` "No table matches.", `estimates`), keeping its `<details>` handling.
- [ ] **Step 4: Run** `cd frontend && npx vitest run components/TableList.test.tsx components/TablePicker.test.tsx && npx tsc --noEmit`. Expected: PASS.
- [ ] **Step 5: Commit** `refactor(data): extract TableList from TablePicker`.

### Task 3: `DatabaseView` driven by the address

**Files:**
- Modify: `frontend/components/DatabaseView.tsx`, `frontend/components/AppShell.tsx` (pass `route`, `navigate`), `frontend/app/legacy.css` (disclosure on narrow screens)
- Modify tests: the `setup` helpers in `frontend/components/DatabaseView.test.tsx` and `DatabaseFlow.test.tsx` (see Step 1); `frontend/components/AppShell.test.tsx` only if it breaks
- Test: new cases in `DatabaseView.test.tsx`

**Interfaces:**
- Consumes: `parseTab`, `tabToSearch`, `DbTab` (Task 1); `TableList` (Task 2); `Navigate` from `lib/useRoute.ts` (`(next: Route, opts?: { replace?: boolean }) => void`).
- Produces: `DatabaseView(props: { me: Me; live: Change[]; route: Route; navigate: Navigate; refreshKey: number; onProposed(c: Change): void; onAskAssistant(text: string): void })`. Child tabs get an extra callback in later tasks: `onOpenTable(name: string): void` (navigates to `{view:"database", schema, table: name}`).

- [ ] **Step 1: Change the test helpers (not the tests).** In both `setup`s render `DatabaseView` inside a small host that calls the real `useRoute()` and passes `route`/`navigate`, after `history.replaceState(null, "", "/database")`; reset `location` to `/database` in `beforeEach`. Run the two files: Expected: they FAIL now (props missing), proving the helper is in place.
- [ ] **Step 2: Write the new failing tests** in `DatabaseView.test.tsx`: `/database` replaces the address with `/database/master`; starting at `/database/crm` selects crm (Overview, `getGraph("crm")`); `/database/ghost` shows "There is no database ghost." and calls no `getGraph("ghost")`; `/database/master/nope` shows "There is no table nope in master." keeps the table list, and `getTable` is not called; `/database/master/customer?tab=relations` opens Relations; `?tab=bogus` shows Fields; clicking a table changes `location.pathname` to `/database/master/customer` and shows Fields; clicking the selected table again returns to `/database/master`; clicking a tab writes `?tab=` into the address without changing the path; switching database navigates to it and keeps `?tab=erd` only when the ERD was open; a reload where the selected table vanished replaces the address with `/database/master`; the left pane sits in a `<details>` titled "Databases and tables".
- [ ] **Step 3: Run** `cd frontend && npx vitest run components/DatabaseView.test.tsx components/DatabaseFlow.test.tsx`. Expected: new cases FAIL.
- [ ] **Step 4: Implement.** Selected database = `route.schema`; table = `route.table`; unknown-ness is judged against `schemas` and the loaded `graph` (message text per Global Constraints; `lastGood` ref marks a table that was present, so a vanished one falls back while a never-present one shows the message). The tab is held as `{ key: "<db>/<table>", tab }` and computed while rendering as `held.key === key ? held.tab : parseTab(location.search, !!table)`; setting it calls `history.replaceState` and updates the held value. Replace `resetTo`/`pickSchema`/`pickTable` with `navigate` calls (create-then-select and vanished-database use `navigate`, the latter with `{ replace: true }`); the schema-change resets (graph, filter, panels kept or closed as today) move into an effect on `schema` that runs before the graph fetch. Replace the databases list buttons' behaviour only; keep their text `"<name> (<n>)"`. The tables list is `TableList` with `searchLabel="Filter tables"`, `emptyText="No tables yet."`, `noMatchText="No matching tables."`, `estimates={false}`, `listLabel="Tables"`. Header actions become `ui` `Button`s (danger variant for Delete and Drop table) with the same names. Wrap the left pane in `<details className="db-rail-box" open>` with `<summary>Databases and tables</summary>`, closed after a choice on narrow screens like `TablePicker`. `AppShell` passes `route` and `navigate`. Keep `DatabaseView` as one file: the spec's optional `DbRail`/`DbHeader` split is not done because the panel state is shared across both and a split would only move props around (ledger as a ruling).
- [ ] **Step 5: Run** `cd frontend && npx vitest run components/DatabaseView.test.tsx components/DatabaseFlow.test.tsx components/AppShell.test.tsx && npx tsc --noEmit`. Expected: PASS (existing assertions unchanged).
- [ ] **Step 6: Commit** `feat(database): address-driven Database tab with shared table list`.

### Task 4: Fields in the new look, and the row `Menu`

**Files:**
- Create: `frontend/components/ui/Menu.tsx` (export from `ui/index.ts`)
- Modify: `frontend/components/FieldsTab.tsx`, `frontend/app/legacy.css`
- Test: `frontend/components/ui/ui.test.tsx` (Menu cases), `frontend/components/FieldsTab.test.tsx`, `frontend/components/DatabaseFlow.test.tsx`

**Interfaces:**
- Produces: `Menu(props: { label: string; items: { label: string; ariaLabel?: string; danger?: boolean; onSelect(): void }[] })`. A trigger `Button` (icon `more`, `aria-label={label}`, `aria-haspopup="true"`, `aria-expanded`); when open a list of plain `<button>`s (each with `aria-label={ariaLabel ?? label}`), closed by choosing an item, Escape (focus returns to the trigger) or a click outside.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Write the failing tests.** Menu: opens on click; Escape closes and focuses the trigger; outside click closes; choosing calls `onSelect` once and closes; items render nothing while closed. FieldsTab (changed existing tests, listed): every test that clicked `Rename|Type|Comment|Drop <column>` first clicks `More actions for <column>` (add a `choose(column, action)` helper); the two `queryByRole(... "created_at")` null assertions stay; the `getByRole("button", { name: "Rename email" })` presence assertion in `FieldsTab.test.tsx:47` and `DatabaseFlow.test.tsx:104` open the menu first. New cases: a viewer has no `More actions` buttons and no Add field; the primary-key column shows a key icon (`getByLabelText("primary key")` within its row) and a unique constraint shows a "unique" badge only when `detail.constraints` has a unique on that single column; the table is inside a `Panel`; "Select a table to see its fields." copy is unchanged.
- [ ] **Step 2: Run** `cd frontend && npx vitest run components/ui/ui.test.tsx components/FieldsTab.test.tsx components/DatabaseFlow.test.tsx`. Expected: new Menu cases and the new FieldsTab cases FAIL; changed ones FAIL until implemented.
- [ ] **Step 3: Implement `Menu`**, then rebuild the `FieldsTab` markup from `ui` parts: sticky header, type and default in the code font, key icon / "unique" badge / muted "null", row actions in one `Menu` per row (items Rename, Type, Comment, Drop with `aria-label` `Rename <column>` etc., Drop `danger`; hidden audit columns get no menu), add-field form inside a `Panel`. Logic, ops and dialogs are unchanged.
- [ ] **Step 4: Run** `cd frontend && npx vitest run components/ui components/FieldsTab.test.tsx components/DatabaseFlow.test.tsx && npx tsc --noEmit`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(database): Fields tab in the new look with a row menu`.

### Task 5: Relations in the new look

**Files:**
- Modify: `frontend/components/RelationsTab.tsx`, `frontend/components/DatabaseView.tsx` (pass `onOpenTable`), `frontend/app/legacy.css`
- Test: `frontend/components/RelationsTab.test.tsx`

**Interfaces:**
- Consumes: `Panel`, `Badge`/`Button` from `ui`; `DatabaseView` supplies `onOpenTable(name: string): void` (Task 3).
- Produces: `RelationsTab` gains the prop `onOpenTable(name: string): void`.

- [ ] **Step 1: Write the failing tests:** *Points to* and *Referenced by* are separate `Panel`s; each relation reads "order.customer_id → customer.id" and has a button "Open customer" / "Open order" that calls `onOpenTable` with that table; a self-reference opens its own table; constraints show their kind as a badge (primary key, unique, check, foreign key); empty panels say "Nothing." / "No constraints." / "No indexes."; viewers see no Add index/unique/foreign key and no Drop buttons; the existing drop constraint/index and add-form tests keep passing (their locators do not move).
- [ ] **Step 2: Run** `cd frontend && npx vitest run components/RelationsTab.test.tsx`. Expected: new cases FAIL.
- [ ] **Step 3: Implement:** three `Panel`s (*Points to*, *Referenced by*, *Indexes and constraints*), forms inside a `Panel`; behaviour of the forms and `ProposeDialog` unchanged.
- [ ] **Step 4: Run** `cd frontend && npx vitest run components/RelationsTab.test.tsx components/DatabaseView.test.tsx && npx tsc --noEmit`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(database): Relations tab in the new look`.

### Task 6: Overview in the new look

**Files:**
- Modify: `frontend/components/OverviewTab.tsx`, `frontend/components/DatabaseView.tsx` (pass `onOpenTable`), `frontend/app/legacy.css`
- Test: `frontend/components/OverviewTab.test.tsx`

**Interfaces:**
- Produces: `OverviewTab` gains `onOpenTable(name: string): void`.

- [ ] **Step 1: Write the failing tests:** stat tiles show tables, columns, relations and approximate rows (sum of `rowEstimate`, formatted with `toLocaleString`) with labels; while `graph` is null a `Skeleton` shows; the table list shows each table with its column count and row estimate and clicking one calls `onOpenTable`; a table name with markup renders as text; the database comment shows; the search box and "Ask the assistant about <db>" (editors only) behave as before (existing tests keep passing).
- [ ] **Step 2: Run** `cd frontend && npx vitest run components/OverviewTab.test.tsx`. Expected: new cases FAIL.
- [ ] **Step 3: Implement** the tiles (a small local `Stat` inside the file), the table list, keep `SearchBox` and the assistant button.
- [ ] **Step 4: Run** `cd frontend && npx vitest run components/OverviewTab.test.tsx components/DatabaseView.test.tsx && npx tsc --noEmit`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(database): Overview in the new look`.

### Task 7: ERD polish, docs and the full check

**Files:**
- Modify: `frontend/components/SchemaView.tsx`, `frontend/components/DatabaseView.tsx` (pass `selected table` and `onOpenTable` to `SchemaView`), `frontend/app/legacy.css`, `README.md`
- Test: `frontend/components/SchemaView.test.tsx`

**Interfaces:**
- Produces: `SchemaDiagram(props: { graph: SchemaGraph; selected?: string | null; onSelect?(name: string | null): void })`; `TablePanel(props: { graph: SchemaGraph; name: string; onOpenFields?(name: string): void })`; `SchemaView(props: { schema: string; refreshKey: number; table?: string | null; onOpenTable?(name: string): void })` where `table` seeds the selection (and follows it when the left pane's table changes).

- [ ] **Step 1: Write the failing tests** (`SchemaView.test.tsx`): 2D cards are `role="button"` with `aria-pressed` and `aria-label` = table name; clicking (or Enter/Space on) a card selects it, marks the relations that touch it `data-hot="true"` and the others `data-dim="true"`, and shows the panel (columns, *Points to*, *Referenced by*); clicking it again or pressing Escape clears everything; "Open fields" calls `onOpenTable(name)`; selecting a table that then vanishes after a reload clears the panel; names `a b`, `x"y` and `<b>` render as text and `onOpenTable` receives them untouched (the path encoding is `buildPath`'s job; add one assertion in `lib/route.test.ts` that `buildPath({view:"database",schema:"master",table:"a b"})` is `/database/master/a%20b`); the 2D/3D control is two buttons with `aria-pressed` and 3D is absent without WebGL; the "Fit to width" toggle sets `aria-pressed` and a `schema-fit` class on the scroll container; foreign-key columns are marked (`data-fk="true"` on their text); `table` prop seeds the selection; the 3D panel also offers "Open fields".
- [ ] **Step 2: Run** `cd frontend && npx vitest run components/SchemaView.test.tsx lib/route.test.ts`. Expected: new cases FAIL.
- [ ] **Step 3: Implement** the diagram changes (give the `<svg>` a `viewBox`; edges get `data-hot`/`data-dim`; cards become focusable groups with `onKeyDown`), the segmented control, the Fit toggle, and `Open fields` in `TablePanel`; `DatabaseView` passes `table={table}` and `onOpenTable`. Update the README (Database tab address, `?tab=`, ERD selection).
- [ ] **Step 4: Run the full check.** `cd frontend && npx vitest run && npx tsc --noEmit`. Expected: all pass (546+ before this slice's additions). No backend changes, so the backend suite is not required.
- [ ] **Step 5: Commit** `feat(database): ERD selection, highlighted relations and Open fields; docs`.
