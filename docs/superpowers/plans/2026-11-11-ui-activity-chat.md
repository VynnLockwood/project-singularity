# UI rework 2a: Activity (Queue and History) and Chat markdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activity gets a Queue tab and a filterable History tab; chat replies render as safe markdown with copyable code blocks.

**Architecture:** One additive backend field (`more` on `GET /api/changes`). Frontend: pure helpers (`lib/historyFilters.ts`) for filters and live merging, a `HistoryView` pane and an `ActivityView` that hosts Queue and History tabs, and a `ChatMarkdown` component used for assistant bubbles. No router library: the tab is part of `Route` (path), filters live in the query string via the History API.

**Tech Stack:** Django (one view), Next.js 15 / React 19, vitest + Testing Library, new dependencies `react-markdown` (^10) and `remark-gfm` (^4).

**Spec:** `docs/superpowers/specs/2026-11-11-ui-activity-chat-design.md`

## Global Constraints

- No raw HTML from a model reply is ever rendered (no `rehype-raw`, no `dangerouslySetInnerHTML`).
- Links are only `http`, `https`, `mailto`; opened with `target="_blank" rel="noopener noreferrer"`.
- User messages stay plain text.
- The old `/queue` address keeps working and maps to the Queue tab.
- History page size is 50; the server caps `limit` at 200, so Show more raises the limit by 50 up to 200. **Ruling (deviates from spec text "raises by 50"):** at 200 with `more` true the button is replaced by "Showing the latest 200. Narrow the filters to see older changes." The spec is updated in Task 1's docs step.
- Revert is shown only to approvers, only for `status === "applied"` and `reversible === true`, and only when no loaded row has `reverts === change.id` with a status other than `rejected` or `failed`. It asks for confirmation inline (no `window.confirm`).
- Every existing frontend and backend test keeps passing; a test may change only where an assistant message now renders as markdown.
- Commit messages end with the two trailer lines the session attribution reminder gives.

## Review Focus

1. A reply with `<script>`, `<img src=x onerror=...>`, `[x](javascript:alert(1))` or a `data:` link must show as text or an unlinked label, never as an element or a live link. (Task 5)
2. A hand-edited History URL with junk (`?status=bogus&kind=x&mine=maybe`) must behave as "any", not send junk to the server or crash. (Task 3)
3. A slow response for an old filter must not overwrite the list for the current filter; a failed Show more must keep the rows already shown. (Task 4)
4. Revert: double click sends one request; a server refusal (already reverted, not reversible) shows its message and keeps the row; a viewer or editor never sees the button. (Task 4)
5. A wide table or a very long unbroken code line must scroll inside its own container, never widen the chat column. (Task 5; asserted as the scroll wrapper existing, since jsdom has no layout)

---

### Task 1: `more` on the changes list

**Files:**
- Modify: `backend/approvals/views.py` (the `changes` GET branch, lines ~19-24)
- Test: `backend/tests/test_api.py`
- Modify: `docs/superpowers/specs/2026-11-11-ui-activity-chat-design.md` (the Show more paragraph in 2.2)

**Interfaces:**
- Consumes: `service.list_changes(...) -> (list[Change], bool)` (already returns the flag).
- Produces: `GET /api/changes` body `{"changes": [...], "more": bool}`.

- [ ] **Step 1: Write the failing test** in `backend/tests/test_api.py`, named `test_changes_list_says_whether_more_exist`: as `editor`, create three changes (post `CREATE`-style proposals with distinct table names, or `Change` rows via the existing helpers), then assert `GET /api/changes?limit=2` has `len(changes) == 2` and `more is True`, and `GET /api/changes?limit=3` has `len(changes) == 3` and `more is False`.
- [ ] **Step 2: Run it to see it fail.** Run: `cd backend && python -m pytest tests/test_api.py -k more_exist -q`. Expected: FAIL (`KeyError: 'more'`).
- [ ] **Step 3: Implement** in the `changes` view: keep the flag (`items, more = service.list_changes(...)`) and return `{"changes": [...], "more": more}`.
- [ ] **Step 4: Run** the same command plus `python -m pytest tests/test_api.py tests/test_mcp_history.py -q`. Expected: PASS.
- [ ] **Step 5: Spec text.** In the spec's section 2.2, replace the Show more sentence with: Show more raises the limit by 50 up to the server's cap of 200; at the cap, with more rows beyond, it says "Showing the latest 200. Narrow the filters to see older changes."
- [ ] **Step 6: Commit** `feat(api): /api/changes says whether more exist`.

---

### Task 2: API client and route

**Files:**
- Modify: `frontend/lib/api.ts` (`Change` type; new `listChanges`, `revertChange`)
- Modify: `frontend/lib/route.ts`
- Test: `frontend/lib/route.test.ts`, `frontend/lib/api.test.ts` (create if none; otherwise add to the existing one)

**Interfaces:**
- Produces:
  - `Change` gains optional `decidedAt: string | null`, `appliedAt: string | null`, `reversible: boolean | null`, `reverts: number | null`.
  - `export interface ChangeFilters { status?: ChangeStatus; kind?: Change["kind"]; schema?: string; mine?: boolean }`
  - `export const listChanges = (f: ChangeFilters, limit: number) => Promise<{ changes: Change[]; more: boolean }>`: `GET /changes?` with only the set params plus `limit`.
  - `export const revertChange = (id: number) => Promise<Change>`: `POST /changes/${id}/revert`.
  - `Route` activity member becomes `{ view: "activity"; tab?: "history" }`.

- [ ] **Step 1: Write failing tests.** `route.test.ts`: `parseRoute("/activity/history")` equals `{ view: "activity", tab: "history" }`; `buildPath` of that is `/activity/history`; `/activity` and `/queue` still give `{ view: "activity" }`; `/activity/nonsense` gives `{ view: "activity" }` (unknown tab ignored). `api.test.ts` with a mocked `fetch`: `listChanges({ status: "applied", mine: true }, 100)` requests `/api/changes?status=applied&mine=1&limit=100` and returns `{ changes, more }` (default `more` false when the key is absent); `revertChange(7)` POSTs `/api/changes/7/revert`.
- [ ] **Step 2: Run** `cd frontend && npx vitest run lib/route.test.ts lib/api.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.** In `route.ts` take `activity` out of `PLAIN`, parse `a === "history"` into the tab, and build `join("activity", route.tab)`. Add the four `Change` fields and the two functions in `api.ts` (use the existing `call` helper; build the query with `URLSearchParams`).
- [ ] **Step 4: Run** the same command and `npx tsc --noEmit`. Expected: PASS, no type errors (fix any place that built `{ view: "activity" }` against the new type).
- [ ] **Step 5: Commit** `feat(web): api and route for Activity history`.

---

### Task 3: History filters and live merge (pure)

**Files:**
- Create: `frontend/lib/historyFilters.ts`
- Test: `frontend/lib/historyFilters.test.ts`

**Interfaces:**
- Consumes: `Change`, `ChangeStatus`, `ChangeFilters`, `Me` from `lib/api`.
- Produces:
  - `export interface HistoryFilters { status: ChangeStatus | ""; kind: Change["kind"] | ""; db: string; mine: boolean }`
  - `export const DEFAULT_FILTERS: HistoryFilters` (all empty / false)
  - `export function parseFilters(search: string): HistoryFilters`: reads `status`, `kind`, `db`, `mine=1`; any value outside the known sets becomes the empty default.
  - `export function filtersToSearch(f: HistoryFilters): string`: `""` for defaults, else `?status=...&...` with only set keys, in the order status, kind, db, mine.
  - `export function toApiFilters(f: HistoryFilters): ChangeFilters` (db becomes `schema`).
  - `export function matches(c: Change, f: HistoryFilters, me: Me): boolean` (a change with no `schema` counts as `master`).
  - `export function mergeLive(rows: Change[], live: Change[], f: HistoryFilters, me: Me): Change[]`: rows replaced by their fresher live copy; live changes not in `rows` that match `f` and whose id is greater than every id in `rows` are added; result newest (highest id) first, no duplicates.

- [ ] **Step 1: Write failing tests** covering: defaults round-trip to `""`; `?status=applied&mine=1` parses and re-serialises identically; junk (`?status=bogus&kind=x&mine=maybe&db=`) parses to `DEFAULT_FILTERS`; `matches` per filter including `mine` by requester email and a missing `schema` meaning master; `mergeLive` replaces a stale pending row with its applied live copy, adds a newer matching live change at the top, ignores a newer non-matching one, ignores an older live change that is not in rows (it belongs to pages not loaded), and never duplicates.
- [ ] **Step 2: Run** `npx vitest run lib/historyFilters.test.ts`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the module (known status and kind sets as constants at the top).
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): history filters and live merge`.

---

### Task 4: HistoryView, ActivityView and wiring

**Files:**
- Create: `frontend/components/HistoryView.tsx`, `frontend/components/ActivityView.tsx`
- Modify: `frontend/components/AppShell.tsx` (activity branch), `frontend/app/legacy.css` (a short `History` block; reuse tokens)
- Test: `frontend/components/HistoryView.test.tsx`, `frontend/components/ActivityView.test.tsx`; update `frontend/components/AppShell.test.tsx` only if it asserts the activity branch.

**Interfaces:**
- Consumes: `listChanges`, `revertChange`, `getSchemas`, `Change`, `Me` (api); `parseFilters`, `filtersToSearch`, `toApiFilters`, `mergeLive`, `DEFAULT_FILTERS` (Task 3); `ChangeCard`; `Badge`, `EmptyState`, `Skeleton`, `Button`, `useToast`, `Icon` from `components/ui`; `Navigate` from `lib/useRoute`.
- Produces:
  - `HistoryView({ me, live, busy }: { me: Me; live: Change[]; busy?: boolean })`: owns filters (initial from `location.search`, written back with `history.replaceState`), limit, rows, loading/error, expanded row, revert confirmation.
  - `ActivityView({ me, tab, navigate, pending, live, onDecide, busy })`: `tab: "queue" | "history"`, `pending: Change[]` (the decidable queue, as `mine` in AppShell), renders the tab bar (`role="tablist"`, buttons `role="tab"` with `aria-selected`, class `tab`, as DatabaseView does) with a `Badge`-style count on Queue when `pending.length > 0`, then `QueueView` or `HistoryView`.

- [ ] **Step 1: Write failing `HistoryView.test.tsx`** (mock `@/lib/api` `listChanges`, `revertChange`, `getSchemas`). Cases:
  - loads once with default filters and limit 50, shows a row per change (`#id`, status badge, kind, database, requester, first line of the prompt);
  - changing each of the four filters sends the matching request and resets the limit to 50, and updates `location.search`;
  - starting from `?status=applied` pre-selects the filter; junk in the URL loads with defaults;
  - Show more appears only when `more` is true, requests limit 100, keeps old rows if that request fails and shows the error; at limit 200 with `more` true it shows the narrow-the-filters note instead of the button;
  - loading skeleton, empty state with Clear filters, error state with Try again;
  - a stale response for an earlier filter (resolve the first request after the second) does not replace the list;
  - activating a row expands a `ChangeCard` with no Approve/Reject buttons for a decided change;
  - a live pending change that matches is added at the top, one that does not match is not, and a live update turns a listed pending row into applied;
  - Revert: shown for an approver on an applied reversible change with no existing revert row; not shown to an editor or viewer, when `reversible` is false, or when a loaded row has `reverts` equal to this id with status applied or pending (but still shown if that revert row is rejected or failed); clicking asks "Revert change #N?" with Yes and Cancel; Yes calls `revertChange` once even when clicked twice quickly, then reloads the list; a rejection shows the server message and keeps the row.
- [ ] **Step 2: Write failing `ActivityView.test.tsx`:** the Queue tab is selected for `tab="queue"` and shows the pending cards; the badge shows the pending count and is absent at zero; clicking History calls `navigate({ view: "activity", tab: "history" })`; clicking Queue calls `navigate({ view: "activity" })`; a viewer sees both tabs.
- [ ] **Step 3: Run** `npx vitest run components/HistoryView.test.tsx components/ActivityView.test.tsx`. Expected: FAIL.
- [ ] **Step 4: Implement** `HistoryView` and `ActivityView`. Request handling: keep a request counter in a ref and ignore any response whose counter is not the latest. The database filter's options come from `getSchemas()` (names) plus the current value if it is not in the list (a dropped database still has history). Format times with a small local helper (`Intl.RelativeTimeFormat`, title attribute = `toLocaleString()`).
- [ ] **Step 5: Wire AppShell:** the activity branch renders `<ActivityView me={me} tab={route.view === "activity" ? route.tab === "history" ? "history" : "queue" : "queue"} navigate={navigate} pending={mine} live={live} onDecide={changes.decide} busy={busy} />`. Add the CSS.
- [ ] **Step 6: Run** `npx vitest run` (whole frontend) and `npx tsc --noEmit`. Expected: PASS.
- [ ] **Step 7: Commit** `feat(web): Activity with Queue and History tabs`.

---

### Task 5: Chat markdown

**Files:**
- Modify: `frontend/package.json`, `frontend/package-lock.json` (install `react-markdown@^10 remark-gfm@^4`)
- Create: `frontend/components/ChatMarkdown.tsx`
- Modify: `frontend/components/ChatView.tsx` (assistant bubbles), `frontend/app/legacy.css`
- Test: `frontend/components/ChatMarkdown.test.tsx`; adjust `frontend/components/ChatView` tests if one asserts a bubble's raw text.

**Interfaces:**
- Produces: `ChatMarkdown({ text }: { text: string })`, a pure rendering of `text`. Uses `useToast` for the Copy confirmation (works without a provider because the context has a default).

- [ ] **Step 1: Install** `cd frontend && npm install react-markdown@^10 remark-gfm@^4`. Expected: lock file updated, no other dependency changes beyond their trees.
- [ ] **Step 2: Write failing `ChatMarkdown.test.tsx`:**
  - a GFM table renders a `<table>` inside an element with class `md-scroll`; a list renders `<ul><li>`; `**bold**` renders `<strong>`; inline code renders `<code>`;
  - a fenced `sql` block renders `<pre>` with the exact text, a visible "sql" label and a Copy button; clicking Copy calls `navigator.clipboard.writeText` with the exact code (no trailing newline);
  - `# Big` renders an `h3`, `## Mid` an `h4`, `### Small` an `h5`, `#### x` an `h6`; no `h1`/`h2` ever;
  - `<script>alert(1)</script>` and `<img src=x onerror="alert(1)">` produce no `script` or `img` element and the text is visible;
  - `[ok](https://example.com)` is a link with `target="_blank"` and `rel="noopener noreferrer"`; `[mail](mailto:a@b.c)` is linked; `[bad](javascript:alert(1))` and `[bad](data:text/html,x)` render the label as plain text with no `href`.
- [ ] **Step 3: Run** `npx vitest run components/ChatMarkdown.test.tsx`. Expected: FAIL (module missing).
- [ ] **Step 4: Implement** `ChatMarkdown` with `remarkPlugins={[remarkGfm]}`, no raw HTML plugin, and a `components` map: `h1..h6` shifted, `a` safe-link, `table` wrapped in `<div className="md-scroll">`, `pre` as `CodeBlock` (language from the child's `language-*` class, text from the child's string content). In `ChatView`, render `<ChatMarkdown text={m.content} />` for `role === "assistant"` and keep `<p>{m.content}</p>` for the user. CSS: `.bubble[data-role="assistant"] { white-space: normal }`, `.md-scroll { overflow-x: auto }`, code block and table styles from tokens, Copy button visible on hover and `:focus-visible`.
- [ ] **Step 6: Run** `npx vitest run` and `npx tsc --noEmit`. Expected: PASS.
- [ ] **Step 7: Commit** `feat(web): chat replies render markdown`.

---

### Task 6: Docs and full check

**Files:**
- Modify: `README.md` (a short paragraph on Activity History and chat markdown where the UI is described)

- [ ] **Step 1: Edit the README**: Activity has Queue and History; History filters and Revert; chat shows markdown.
- [ ] **Step 2: Run everything.** `cd backend && python -m pytest -q` and `cd ../frontend && npx vitest run && npx tsc --noEmit`. Expected: all green; note the counts for the pull request.
- [ ] **Step 3: Commit** `docs: README for Activity History and chat markdown`, push the branch, open a pull request titled `UI rework 2a: Activity history and chat markdown`. The body lists what changed, every existing test that was changed and why, and what slice 2b will be.
