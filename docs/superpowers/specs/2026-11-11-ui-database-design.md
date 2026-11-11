# UI rework 2c: the Database tab

Slice 2c of piece 2 (view rework) of the UI sweep (see spec 2026-11-10-ui-shell-design-system-design.md). Slices 2a (Activity, Chat) and 2b (Data, PR #34) come first; this slice reuses what 2b adds (`TablePicker` parts, `lib/dataRoute.ts` style of address handling). It covers the Database tab only: no new kinds of change, no backend work.

## 1. What the owner asked for

- A table list and an address (`/database/<db>/<table>`) so a table or tab can be opened by link and survives a reload.
- The Fields, Relations and Overview tabs in the new look.
- ERD polish.

## 2. Address and layout

- **Address.** `/database/<db>/<table>?tab=fields|relations|erd|overview`. The route already parses to `{ view: "database", schema, table }`; the tab is the `tab` query value. Today all of this is component state and is lost on reload.
  - `/database` selects `master` (or the first database if it is gone) and replaces the address.
  - `/database/<db>` shows the database's Overview. `/database/<db>/<table>` shows Fields. A `tab` that is missing or unknown means that default. Fields and Relations need a table; with none they fall back to Overview.
  - An unknown database or table shows an inline message ("There is no table X in Y.") with the lists still usable. It never blanks the page.
  - Choosing a database navigates to it (Overview). Choosing a table navigates to `/database/<db>/<table>` (Fields); choosing the selected table again clears it (as today).
  - A table or database that vanishes after a reload falls back to the database Overview / `master`, replacing the address.
- **Left pane.** Two parts, both kept visible because this is the place databases are managed:
  - Databases: a list with table counts, the current one marked, **New database** (editors) below it.
  - Tables: search box, list, **New table** (editors). The search and list are the same part 2b's `TablePicker` uses, extracted as `components/TableList.tsx` and shared (2b's picker keeps its database select).
  - On screens under 800px the pane collapses into a "Databases and tables" disclosure; choosing something closes it.
- **Header.** `<db> / <table>` on the left. On the right: Rename and Delete (database, editors, not `master`), Rename table and Drop table (editors), Export. They become `ui/` `Button`s (destructive ones `variant="danger"`). All still go through the existing `ProposeDialog` review step; nothing about what is proposed changes.
- The created-database auto-select (a database you just proposed is opened once it exists, only if you are idle) is kept, using `navigate`.

## 3. Tabs in the new look

Tabs use `.db-tabs` / `.tab` as today, with the active tab in the address.

- **Fields.** Same columns and actions (add, rename, change type, comment, drop; audit columns toggle). Built from `ui/` parts: sticky header; the type in the code font; a key icon for the primary key, a "unique" badge, "null" shown as a muted word; the default in the code font; the row actions in one "More" menu per row instead of a row of links; the add-field form in a `Panel`; empty state ("No fields." / "Select a table to see its fields." with a pointer to the left pane).
- **Relations.** Three `Panel`s: *Points to* and *Referenced by* (each relation as "this.column → table.column", on-delete rule, a link that opens that table), and *Indexes and constraints* (kind as a badge: primary key, unique, check, foreign key). Add index / unique / foreign key stay as forms inside a `Panel`. Empty states per panel.
- **Overview.** Stat tiles (tables, columns, approximate rows, relations), the database comment, the schema search (existing `SearchBox`), the table list with row estimates and column counts (each opens that table), and "Ask the assistant about <db>" for editors.
- All text, errors and states use the 2a/2b conventions: `Skeleton` while loading, an error message with Try again, nothing blanks the page.

## 4. ERD polish

- 2D / 3D becomes a two-option segmented control (`aria-pressed`), 3D only where WebGL exists as today.
- **2D diagram.** Table cards are buttons: choosing one selects it (accent border), highlights the relations that touch it and dims the rest, and opens the side panel the 3D view already has (columns, points to, referenced by) with an **Open fields** action that navigates to `/database/<db>/<table>`. Choosing it again or Escape clears. Keyboard: cards are focusable; Enter/Space selects.
- The diagram scrolls inside its container and a "Fit" toggle scales it to the container width (default on for wide schemas). Colours come from the design tokens; foreign-key columns are marked.
- The 3D view keeps its scene; its list and panel are unchanged except that **Open fields** is added to the panel.
- The selected table in the ERD is the same table as the left pane's selection when there is one.

## 5. Frontend contract

- `components/DatabaseView.tsx`: props `{ me, live, route, navigate, refreshKey, onProposed, onAskAssistant }`. Selected database, table and tab come from the address; the rest of the local state (panels, forms, graph, schemas, `created`) stays. File is split where it helps: the left pane becomes `components/DbRail.tsx`, the header actions `components/DbHeader.tsx`.
- `lib/databaseRoute.ts` (new): `parseTab(search): DbTab`, `tabToSearch(tab, hasTable)`; unknown values mean the default.
- `components/TableList.tsx` (new, shared with `TablePicker`).
- `components/FieldsTab.tsx`, `RelationsTab.tsx`, `OverviewTab.tsx`, `SchemaView.tsx`: markup moved to the new parts; props and behaviour of proposals unchanged. `SchemaDiagram` gains `selected`, `onSelect` props.
- `components/ui`: a small `Menu` (button + list, closes on Escape / outside click) if no existing part fits the row actions.
- `AppShell` passes `route` and `navigate` to `DatabaseView`; the command palette and search links that open a table use `navigate({view:"database", schema, table})`.

## 6. Out of scope

New kinds of proposal, editing a table's comment, reordering fields, changes to the 3D scene, an ERD export image, saved ERD layouts, Settings / Scripts / Connect / Duplicates views.

## 7. Testing

- `lib/databaseRoute.test.ts`: tab parse and build; junk means default.
- `TableList.test.tsx`: search narrows, selected is marked, empty and no-match messages.
- `DatabaseView.test.tsx` (existing, 414 lines): keeps passing except where a locator moved; new cases: `/database` selects `master` and replaces the address; `/database/x` and `/database/master/nope` show the message and keep the lists; `?tab=relations` opens Relations; choosing a table navigates; a vanished table falls back; a created database opens once it exists and only when idle (existing test moved to navigate).
- Fields / Relations / Overview tests keep passing; new cases: the "More" menu offers rename, type, comment, drop and opens the right form; Relations shows both directions and a link that navigates; Overview tiles and table list.
- `SchemaView.test.tsx`: selecting a card highlights its relations and shows the panel; Escape clears; Open fields navigates.
- `npm test` and `tsc --noEmit` green; no backend changes.

## 8. Risks

- `DatabaseView` has the most intertwined state in the app (panels, created-database tracking, stale-request counters). Moving schema/table/tab into the address touches all of it; the plan changes it in small steps with the existing tests as the guard.
- Row action menus must stay reachable by keyboard and not hide actions from viewers (viewers still see no actions).
- 2c depends on 2b's `TablePicker`; the plan starts after 2b is merged.
