# UI rework 2b: the Data tab

Slice 2b of piece 2 (view rework) of the UI sweep (see spec 2026-11-10-ui-shell-design-system-design.md). Slice 2a (Activity, Chat) is merged. Slice 2c (the Database tab: table list, Fields / Relations / Overview, ERD) follows. This slice covers the Data tab only.

## 1. What the owner asked for

- A database picker and a table list with search; `/data/<db>/<table>` opens a table directly and survives a reload.
- The grid in the new look.
- A visible filter and sort bar.
- Export of rows to CSV.

Sort and filters already exist in the API (`sort`, `dir`, `filters`, `inactive`), so those parts are front end only. CSV needs one backend endpoint.

## 2. Layout and address

The Data view has a left pane and the grid beside it.

- **Left pane.** A database select (the managed schemas, from `getSchemas`), a search box that filters the table list as you type, and the table list for the chosen database (from `getGraph(schema)`), each with its row estimate if the API gives one. The selected table is highlighted. The pane scrolls on its own.
- **Narrow screens (under 800px).** The pane collapses into a "Tables" disclosure above the grid; choosing a table closes it.
- **Address.** `/data/<db>/<table>`. The shell already parses it into `{ view: "data", schema, table }`; the view now reads it and writes it with `navigate`.
  - `/data` picks the first database that has tables and its first table, and replaces the address (no extra history entry).
  - `/data/<db>` picks that database's first table.
  - An unknown database or table shows an inline message ("There is no table X in Y.") with the list still usable. It never blanks the page.
  - Switching database selects its first table. A table that disappears after a reload of the list falls back the same way.
- **State in the query string:** `sort`, `dir`, `inactive=1`, and one `f.<column>=<text>` per filter. A link reproduces the view. Page number and page size are not in the address (a shared link opens at the first page).

## 3. The grid in the new look

Same features, nothing removed: edit cells, add rows, remove and restore rows, reference columns as dropdowns with labels, show inactive, show labels or ids, column stats, page size, previous and next.

- Built from the `ui/` parts (`Button`, `Badge`, `EmptyState`, `Skeleton`, `ProgressBar`, `Icon`); the old class names that remain keep working.
- Sticky header row inside the scroll area; the actions column stays visible when scrolling sideways.
- **Pending edits** are visible at a glance: an edited cell is tinted, a removed row is dimmed with a strikethrough and a restore button, new rows sit at the top with a "new" badge, and an invalid cell shows an icon and its message (not colour alone).
- **Save bar.** A bar pinned to the bottom of the grid shows "N pending changes", the count of cells that need fixing, Discard and "Save for approval". It is only present when there is something pending.
- **Toolbar.** Table name and range ("51–100 of 1,240") on the left; on the right Add row, Export CSV (editors and approvers), page size, show labels, show inactive, Column stats. Previous / Next sit under the grid with the range.
- States: loading (skeleton, then a progress bar on reloads), empty table ("No rows yet." with Add row for editors), empty result ("No rows match these filters." with Clear filters), error (message, Try again).
- Keyboard: every control and cell input is reachable by Tab; sort headers are buttons with `aria-sort`; focus is visible.

## 4. Filter and sort bar

The per-column filter row under the header is replaced by a bar above the grid.

- **Add filter**: pick a column, type text, press Enter or Add. Each filter shows as a chip, "name contains 'abc'" with a remove button. Filters mean what the server does today: the column's text contains the typed text, case-insensitive. Several chips combine with AND.
- **Sort**: click a header to sort ascending, click again for descending, a third click clears the sort. A "Sorted by name ↑" chip with a remove button shows while a sort is active.
- **Clear all** appears when there is at least one chip.
- Applying a filter or sort goes back to the first page and keeps unsaved edits (they belong to rows, not to the view). If an edited row is no longer in view, the Save bar still counts it.

## 5. Export CSV

- **Button** "Export CSV" in the toolbar, shown to editors and approvers (data export already needs the editor role, see `schema/views.py: export`).
- **What is exported**: the whole filtered, sorted view, not just the visible page, with the same columns the grid shows and raw stored values (a reference column exports its id, not its label).
- **Endpoint**: `GET /api/schema/tables/<table>/export.csv` with `schema`, `sort`, `dir`, `filters` (JSON, as the rows endpoint), `inactive`. Editor role. Streams the response; filename `<schema>-<table>-<YYYY-MM-DD>.csv`.
- **Cap**: the existing setting `limits.maxExportRows`. If the filtered view has more rows than the cap the endpoint answers 400 "N rows match; the most that can be exported is M (setting limits.maxExportRows). Narrow the filters." and the UI shows that message. It never truncates silently.
- **Format**: UTF-8 with a byte order mark (so Excel reads Thai text correctly), a header row, RFC 4180 quoting, `\r\n` line ends, `NULL` as an empty field. Text values that start with `=`, `+`, `-`, `@`, a tab or a carriage return get a leading apostrophe so a spreadsheet cannot run them as a formula; numeric and boolean columns are never altered.
- **Audit**: one `table.export_csv` entry with the table, schema, filter summary and row count.
- The grid's shared query logic (filters, sort, inactive) is factored into one helper that both `read_rows` and the CSV export use, so the two cannot drift.

## 6. Frontend contract

- `components/DataView.tsx`: left pane and grid host; props `{ me, live, route, navigate, refreshKey, onProposed, onBusy }`.
- `components/TablePicker.tsx` (new): database select, search, list.
- `components/GridEditor.tsx`: keeps its data logic; markup moves to the new parts; gains `view` state in and out (`sort`, `dir`, `filters`, `inactive`) through props so the address owns it.
- `components/FilterBar.tsx` (new): chips and the add-filter control.
- `lib/dataRoute.ts` (new): pure helpers to turn the address query string into view state and back (`parseView`, `viewToSearch`), unknown or junk values meaning "none".
- `lib/api.ts`: `csvUrl(table, schema, view)` builds the download link (a plain href lets the browser stream it, like `exportUrl`).
- `AppShell` passes `route` and `navigate` to `DataView`.

## 7. Backend touch

One view and one helper in `backend/schema`: `rows.export_csv(...)` (generator of CSV chunks) and `schema_views.table_export_csv`, URL `api/schema/tables/<name>/export.csv`. The shared where/order helper is a refactor of `read_rows` protected by the existing rows tests.

## 8. Out of scope

A row detail drawer, multi-row selection, hiding or reordering columns, copying cells, CSV import, XLSX export, saving a filter as a named view, page and page size in the address, the Database tab (slice 2c), Settings, Scripts, Connect and Duplicates.

## 9. Testing

- `lib/dataRoute.test.ts`: round trip of sort, dir, inactive and filters; junk and repeated keys mean "none"; column names with dots and spaces survive.
- `TablePicker.test.tsx`: lists databases and tables; search narrows the list; choosing a table navigates; a database with no tables says so.
- `DataView.test.tsx`: `/data` selects the first table and replaces the address; an unknown table shows the message and keeps the list; a link with `?sort=name&f.name=ab` loads with them applied; switching database selects its first table.
- `FilterBar.test.tsx`: adding, removing and clearing chips; Enter adds; sort chip and third-click clear.
- `GridEditor` tests keep passing except where a locator moved with the markup; new cases: pending edits survive a filter change, the Save bar appears only with pending edits, Export CSV hidden from a viewer, an export error message is shown.
- Backend: CSV has the BOM, the header and quoting; filters, sort and inactive match the rows endpoint; over the cap gives 400 with the message; a formula-looking string is prefixed and a number is not; Thai text round-trips; a viewer gets 403; an unknown table or column gets 400/404 like the rows endpoint; the audit entry is written; `read_rows` behaviour unchanged (existing tests).
- `npm test`, `tsc --noEmit` and the whole backend suite are green before the pull request.

## 10. Risks

- Moving the grid's markup touches the file with the most tests; the plan keeps the data logic and changes markup in small steps.
- CSV injection defence changes some strings in the export (a leading apostrophe). It is documented in the README and limited to text columns.
- Large exports hold a read connection for a while: streaming, the row cap and the existing statement timeout bound this.
