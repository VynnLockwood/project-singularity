# UI rework 2a: Activity (Queue and History) and Chat markdown

Slice 2a of piece 2 (view rework) of the UI sweep. Spec 2026-11-10-ui-shell-design-system-design.md describes the sweep: piece 1 (shell, merged), piece 2 (views, in slices), piece 3 (backend additions: chat streaming, home stats, deeper health check), piece 4 (backend health). Slice 2a covers two views only; the rest of piece 2 (Data grid, Database and ERD, Settings, Scripts, Connect, Duplicates) are later slices.

## 1. What the owner asked for

- Activity shows what is waiting (Queue) and what already happened (History).
- Chat replies render as formatted text: lists, tables, bold, and SQL in code blocks.
- Chat streaming is NOT in this slice (piece 3). Audit log, backups list, conversation rename/delete/search and URL-per-conversation were offered and not chosen.

## 2. Activity

### 2.1 Layout

`/activity` shows two tabs, **Queue** (default) and **History**. `/activity/history` opens History directly. The old `/queue` address keeps working.

- Queue: what `QueueView` shows today (pending changes the person could decide, as `ChangeCard`s), moved onto the `ui/` parts. The tab label carries the pending count as a `Badge` when it is above zero.
- History: a filterable, newest-first list of every change.

### 2.2 History

Filters, all optional, kept in the query string so a link reproduces the view: `status` (any, pending, applied, rejected, failed, stale), `kind` (any, schema, data, config, sql), `db` (a database/schema name, or any), `mine` (only my changes).

Each row shows: `#id`, a status `Badge`, kind, database, tables (first two, then "+n"), requester, relative time (full timestamp in the title attribute), and the first line of the prompt. A row is a button; activating it expands the same `ChangeCard` the Queue uses, read-only for a decided change (no approve/reject buttons; the card already hides them when status is not pending). The card for an applied, reversible change also offers **Revert** to approvers (`POST /api/changes/<id>/revert`, which the backend already has). After a revert the list refreshes and shows the new change.

The list loads 50 rows. A **Show more** button raises the limit by 50 up to the server's cap of 200 and reloads; it shows only when the server says more exist. At the cap, with more rows beyond, it says "Showing the latest 200. Narrow the filters to see older changes." Changing a filter resets the limit to 50.

States: loading (skeleton rows), empty ("No changes match these filters." with a Clear filters button), error (message and Try again). A change that arrives live (WebSocket event) and matches the filters appears at the top without a reload.

Permissions: every signed-in role may read History (Activity is already a viewer item). Revert is shown to approvers only, matching the server.

### 2.3 Backend touch (additive, one line)

`GET /api/changes` already filters by `status`, `kind`, `schema`, `mine` and `limit`, but throws away the "more exist" flag that `list_changes` computes. The endpoint adds `"more": <bool>` to its response. Nothing else changes; existing callers ignore the extra key. One backend test pins it.

### 2.4 Frontend contract

- `lib/api.ts`: `Change` gains `decidedAt`, `appliedAt`, `reversible`, `reverts` (all already sent by the server). New `listChanges(filters, limit) -> {changes, more}` and `revertChange(id)`.
- `lib/route.ts`: `{ view: "activity"; tab?: "history" }`, parsed from `/activity/history`, built back the same way. Filters live in the query string, read and written by the History component through the router, not through `Route`.
- `components/ActivityView.tsx` (new): tab bar and the two panes. `QueueView.tsx` stays the Queue pane. `HistoryView.tsx` (new) is the History pane. `AppShell` renders `ActivityView` for the activity view.

## 3. Chat markdown

Assistant messages render through `react-markdown` with `remark-gfm` (tables, strikethrough, task lists, autolinks). User messages stay plain text (what was typed is what is shown).

- No raw HTML: `react-markdown`'s default, kept (no `rehype-raw`).
- Links open in a new tab with `rel="noopener noreferrer"`; only `http`, `https` and `mailto` URLs are linked, anything else renders as text.
- Fenced code blocks render in a `<pre><code>` with the design system's code style, a language label when the fence names one, and a **Copy** button (visible on hover and on keyboard focus, with a toast "Copied").
- Tables scroll horizontally inside their own container; they never widen the chat.
- Headings are mapped down one level (a model's `#` becomes the chat's section heading size) so replies cannot shout.
- Rendering is a pure function of the message text, so it is correct for a reply that streams in later (piece 3) with no change here.

New dependencies: `react-markdown` and `remark-gfm`. Added to `package.json`, lock file updated, nothing else.

## 4. Out of scope

Streaming; markdown in the user's own messages; syntax highlighting (language label only); Audit log and Backups lists; conversation URL, rename, delete, search; pagination beyond Show more; Activity history for configuration of other databases beyond the `db` filter; any change to how changes are proposed, approved or reverted.

## 5. Testing

- `route.test.ts`: `/activity/history` round-trips; `/queue` still maps to the Queue.
- `HistoryView.test.tsx`: loads with the default filters; each filter changes the request; Show more appears only when `more` is true and raises the limit; empty, loading and error states; a live event that matches the filters is added, one that does not is not; expanding a row shows the card without approve/reject; Revert is shown to an approver on a reversible applied change and not to an editor or on an irreversible one.
- `ActivityView.test.tsx`: tab switching updates the URL; the Queue badge shows the pending count.
- `ChatMarkdown.test.tsx`: a table, a list, bold, inline code and a fenced SQL block render as the matching elements; `<script>` and `<img onerror>` in a reply render as text, not elements; a `javascript:` link is not linked; Copy puts the exact code text on the clipboard.
- Backend: one test that `/api/changes` returns `more` true and false at the limit boundary.
- The existing ChatView and QueueView tests keep passing unchanged except where a message now renders as markdown.
- `npm test`, `tsc --noEmit` and the whole backend suite are green before the pull request.

## 6. Risks

- `react-markdown` is a new dependency: pinned to a major version; its output is the only place model text becomes elements, so the no-raw-HTML test is the guard.
- History can be long on an old install: the 50-row cap and Show more bound the work; the endpoint is already indexed by creation order.
- Revert is destructive by nature: the button asks for confirmation before it sends, and the server remains the authority on what can be reverted.
