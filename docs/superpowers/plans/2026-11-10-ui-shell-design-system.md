# UI Shell and Design System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the frame and the styles every Singularity view sits in: tokens, fonts, side rail, URL state, command palette, toasts, review tray, state-aware black hole, new sign-in screen.

**Architecture:** One optional catch-all Next route serves the same client app; a pure `lib/route.ts` plus the History API holds the place. `AppShell` is split into `Rail`, `TopBar`, `ReviewTray` and a `useChanges` hook. Styles move to token-based CSS files; old class names keep working through `legacy.css`, so no view is rewritten here.

**Tech Stack:** Next 15 (App Router), React 19, TypeScript, Vitest + Testing Library (jsdom), plain CSS with custom properties, `@fontsource/*` fonts. No UI library.

**Spec:** `docs/superpowers/specs/2026-11-10-ui-shell-design-system-design.md`

## Global Constraints

- Working directory for every command: `frontend/`. Branch: `ui/shell-design-system`. Commits authored as the repository owner (already set in local git config); no co-author trailers.
- Backend, engine, embed, nginx and compose files are not touched.
- No hex colour outside `app/tokens.css` in any file this plan creates or edits. (Canvas drawing code in `Cosmos.tsx` and `UniverseScene.tsx` is exempt.)
- New runtime dependencies are exactly: `@fontsource/chakra-petch`, `@fontsource/ibm-plex-sans`, `@fontsource/ibm-plex-sans-thai`, `@fontsource/ibm-plex-mono`. Nothing else.
- Styles are plain `.css` files imported from `app/layout.tsx`; new classes are prefixed `ui-` or `shell-`. No CSS modules (Vitest does not resolve their class names).
- Existing class names (`button`, `link`, `tab`, `badge`, `muted`, `small`, `problems`, `warnings`, `change`, `diff`, `grid`, and the rest of today's `globals.css`) must keep working.
- UI copy: sentence case, no "please", no "successfully", no exclamation marks. Status always shows an icon and a word, never colour alone.
- All motion is disabled under `prefers-reduced-motion: reduce`.
- Baseline is 34 test files, 320 tests, all passing. After every task: `npm test && npm run typecheck` pass. An existing test changes only where this plan names it.
- The `<nav aria-label="Main">` landmark stays, and nav items stay `button` elements named `Chat`, `Database`, `Data`, `Duplicates`, `Scripts`, `Connect`, `Settings` (existing tests query them by role and name).

## Review Focus

1. **Burst of live events.** Ten changes arrive in one second: at most three toasts show, older ones are dropped, the page never gets covered. (Test in Task 3 and Task 6.)
2. **Shortcut keys while typing.** `g r` and other single-key shortcuts do nothing while focus is in an `input`, `textarea`, `select` or `contenteditable`; `⌘K` works everywhere. (Tests in Task 6 and Task 8.)
3. **Odd paths.** `/data/master/my%20table`, a trailing slash, an empty segment, `/chat/abc`, `/nope`: parse without throwing; unknown gives the not-found route. (Test in Task 4.)
4. **Palette with no schema data.** `getSchemas` or `getGraph` rejects or is still loading: views and actions are still listed and usable. (Test in Task 8.)
5. **A change decided elsewhere while its card is open in the tray.** The card updates to the new status, leaves the pending list, focus stays inside the tray, nothing throws. (Test in Task 6.)

---

### Task 1: Tokens, fonts and the CSS split

**Files:**
- Create: `app/tokens.css`, `app/base.css`, `app/legacy.css`, `app/shell.css`, `app/ui.css` (the last two start empty with a header comment), `lib/contrast.ts`, `lib/contrast.test.ts`
- Modify: `app/layout.tsx`, `package.json`, `package-lock.json`
- Delete: `app/globals.css`

**Interfaces:**
- Produces: CSS custom properties exactly as named in spec section 1.1, plus `--font-title`, `--font-text`, `--font-code`, `--space-1..6` (4, 8, 12, 16, 24, 32 px), `--radius-control` (6px), `--radius-panel` (10px), `--dur-fast` (120ms), `--dur-base` (200ms). `contrastRatio(a: string, b: string): number` and `readTokens(css: string): Record<string, string>` in `lib/contrast.ts`.

- [ ] **Step 1: Write the failing test** `lib/contrast.test.ts`

```ts
import { readFileSync } from "node:fs";
import path from "node:path";
import { contrastRatio, readTokens } from "./contrast";

const t = readTokens(readFileSync(path.resolve(__dirname, "../app/tokens.css"), "utf8"));
const TEXT_PAIRS: [string, string][] = [
  ["--text", "--void"], ["--text", "--surface-1"], ["--text", "--surface-2"], ["--text", "--surface-3"],
  ["--text-bright", "--surface-1"], ["--text-dim", "--void"], ["--text-dim", "--surface-2"],
  ["--text-faint", "--void"], ["--text-faint", "--surface-1"],
  ["--on-disk", "--disk"], ["--blueshift", "--blueshift-tint"], ["--redshift", "--redshift-tint"],
  ["--disk-hot", "--disk-tint"], ["--blueshift", "--surface-1"], ["--redshift", "--surface-1"],
];

it("black on white is 21:1", () => expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 1));
it.each(TEXT_PAIRS)("%s on %s meets WCAG AA for text", (fg, bg) =>
  expect(contrastRatio(t[fg], t[bg])).toBeGreaterThanOrEqual(4.5));
it("UI marks meet 3:1", () => {
  expect(contrastRatio(t["--disk"], t["--void"])).toBeGreaterThanOrEqual(3);
  expect(contrastRatio(t["--line-strong"], t["--void"])).toBeGreaterThanOrEqual(1.5); // hairline, decorative
});
it("defines every colour token in the spec", () => {
  for (const k of ["--void","--surface-1","--surface-2","--surface-3","--line","--line-strong","--text","--text-bright","--text-dim","--text-faint","--disk","--disk-hot","--blueshift","--redshift","--blueshift-tint","--redshift-tint","--disk-tint","--on-disk"])
    expect(t[k]).toMatch(/^#[0-9a-f]{6}$/i);
});
```

- [ ] **Step 2:** Run `npx vitest run lib/contrast.test.ts`. Expected: FAIL, module not found.
- [ ] **Step 3:** Implement `lib/contrast.ts` (WCAG relative luminance; `readTokens` collects `--name: #hex;` pairs from the `:root` block) and `app/tokens.css` with the spec's values. If a pair fails, adjust the token value (lighten text or darken surface) until it passes and note the final value in the commit message.
- [ ] **Step 4:** `npm install @fontsource/chakra-petch @fontsource/ibm-plex-sans @fontsource/ibm-plex-sans-thai @fontsource/ibm-plex-mono`. In `app/layout.tsx` import: Chakra Petch 600; Plex Sans 400 and 500; Plex Sans Thai 400 and 500; Plex Mono 400; then `tokens.css`, `base.css`, `shell.css`, `ui.css`, `legacy.css` in that order. `--font-text: "IBM Plex Sans", "IBM Plex Sans Thai", system-ui, sans-serif`.
- [ ] **Step 5:** Write `base.css` (box sizing, body on `--void` with 14px `--font-text`, headings in `--font-title`, form controls on `--surface-2` with `--line-strong` border, `:focus-visible` ring `2px solid var(--disk)` offset 2px, `.sr-only`, a reduced-motion block that sets `animation: none !important; transition: none !important`). Move every rule from `globals.css` into `legacy.css`, replacing each hex with the nearest token; map the status colours: applied and add to `--blueshift`, rejected, failed and remove to `--redshift`, change and warnings to `--disk-hot`. Delete `globals.css`.
- [ ] **Step 6:** Run `grep -nE "#[0-9a-fA-F]{3,8}\b" app/base.css app/legacy.css app/shell.css app/ui.css`. Expected: no output.
- [ ] **Step 7:** Run `npm test && npm run typecheck && npm run build`. Expected: 35 files pass, build succeeds.
- [ ] **Step 8:** Commit: `feat(ui): design tokens, self-hosted fonts, token-based styles`.

### Task 2: Shared parts

**Files:**
- Create: `components/ui/Icon.tsx`, `Button.tsx`, `Badge.tsx`, `Panel.tsx`, `Skeleton.tsx`, `ProgressBar.tsx`, `EmptyState.tsx`, `Kbd.tsx`, `index.ts`, `ui.test.tsx` (all under `components/ui/`)
- Modify: `app/ui.css`

**Interfaces:**
- Produces (all exported from `@/components/ui`):
  - `type IconName = "chat" | "database" | "table" | "copy" | "script" | "activity" | "plug" | "settings" | "search" | "check" | "x" | "clock" | "alert" | "plus" | "chevron-right" | "chevron-down" | "menu" | "logout" | "external" | "refresh" | "inbox" | "undo" | "dot" | "more"`; `Icon({ name, size = 16, label? })` renders inline SVG, `aria-hidden` unless `label` is given (then `role="img"` and `aria-label`).
  - `Button({ variant = "secondary", size = "md", icon?, href?, ...rest })`, variants `"primary" | "secondary" | "ghost" | "danger"`, sizes `"sm" | "md"`; `<a>` when `href` is set, else `<button type="button">` unless `type` is passed.
  - `type Status = "pending" | "applied" | "rejected" | "failed" | "stale" | "reverted"`; `statusMeta(status: string): { tone: "wait" | "ok" | "bad" | "idle"; icon: IconName; label: string }`; `Badge({ status })` shows icon and label with `data-tone`.
  - `Panel({ title?, actions?, children, as = "section" })`, `Skeleton({ rows = 3 })` (`aria-hidden`), `ProgressBar({ active, label = "Loading" })` (`role="progressbar"` only while active), `EmptyState({ icon, title, children, action? })`, `Kbd({ children })`.

- [ ] **Step 1: Write the failing tests** in `components/ui/ui.test.tsx`:
  - `Icon` without label has `aria-hidden="true"`; with `label="Applied"` is found by `getByRole("img", { name: "Applied" })`.
  - `Button` with `href="/x"` renders a link; without it a button with `type="button"`; `variant="danger"` sets `data-variant="danger"`.
  - `statusMeta("applied")` is `{ tone: "ok", icon: "check", label: "Applied" }`; `"pending"` is tone `wait`, label `Waiting`; `"rejected"` and `"failed"` are tone `bad`; an unknown string gives tone `idle` and the string itself as label.
  - `Badge status="rejected"` contains the text `Rejected` and an icon.
  - `ProgressBar active={false}` renders no `progressbar` role; `active` renders one named `Loading`.
  - `EmptyState` renders its title as a heading and its action.
- [ ] **Step 2:** Run `npx vitest run components/ui/ui.test.tsx`. Expected: FAIL.
- [ ] **Step 3:** Implement the parts and their `ui-*` classes in `app/ui.css`. Icons are 24x24 viewBox, `stroke="currentColor"`, stroke width 1.75, no fill. Tones map to tokens: `ok` to `--blueshift`, `wait` to `--disk`, `bad` to `--redshift`, `idle` to `--text-dim`.
- [ ] **Step 4:** Run `npm test && npm run typecheck`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(ui): shared parts (Button, Badge, Panel, Icon, feedback)`.

### Task 3: Toasts

**Files:**
- Create: `components/ui/Toast.tsx`, `components/ui/Toast.test.tsx`
- Modify: `components/ui/index.ts`, `app/ui.css`

**Interfaces:**
- Produces: `ToastProvider({ children })`; `useToast(): { success(msg: string, opts?: ToastOpts): void; error(msg: string, opts?: ToastOpts): void; info(msg: string, opts?: ToastOpts): void }` with `ToastOpts = { action?: { label: string; onClick: () => void } }`. Outside a provider `useToast()` returns no-op functions (so existing component tests need no wrapper). Constants `MAX_TOASTS = 3`, `TOAST_MS = 5000`.

- [ ] **Step 1: Write the failing tests:**
  - `success("Saved")` shows the text inside a `role="status"` element; `error("Could not save")` inside `role="alert"`.
  - With fake timers: a success toast is gone after 5000 ms; an error toast is still there after 60000 ms and goes when its `Dismiss` button is clicked.
  - Ten `info` calls in a row leave exactly three toasts, and they are the last three. (Review Focus 1)
  - An `action` renders a button with its label; clicking it calls `onClick` and removes the toast.
  - `useToast()` with no provider does not throw.
- [ ] **Step 2:** Run the file. Expected: FAIL.
- [ ] **Step 3:** Implement. Viewport is fixed bottom right, above the phone bottom bar (`bottom: calc(var(--space-4) + var(--shell-bottom, 0px))`).
- [ ] **Step 4:** Run `npm test && npm run typecheck`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(ui): toasts`.

### Task 4: Routes

**Files:**
- Create: `lib/route.ts`, `lib/route.test.ts`, `lib/useRoute.ts`, `lib/useRoute.test.tsx`, `app/[[...path]]/page.tsx`
- Delete: `app/page.tsx`

**Interfaces:**
- Produces:
  ```ts
  export type View = "chat" | "database" | "data" | "duplicates" | "scripts" | "activity" | "connect" | "settings";
  export type Route =
    | { view: "chat"; conversation?: number }
    | { view: "database" | "data"; schema?: string; table?: string }
    | { view: "settings"; section?: "quality" | "rules" | "team" }
    | { view: "duplicates" | "scripts" | "activity" | "connect" }
    | { view: "notfound"; path: string };
  export function parseRoute(pathname: string): Route;
  export function buildPath(route: Route): string;
  export function useRoute(): [Route, (next: Route, opts?: { replace?: boolean }) => void];
  ```
- `useRoute` reads `window.location.pathname`, writes with `history.pushState` or `replaceState`, and listens to `popstate`. It does not use `next/navigation`, so components render in tests without a router.

- [ ] **Step 1: Write the failing tests** `lib/route.test.ts`:
  - `parseRoute("/")` is `{ view: "chat" }`; `"/chat/12"` has `conversation: 12`; `"/chat/abc"` is `{ view: "chat" }`.
  - `"/database/master/province"` and `"/data/sales/customer"` carry schema and table; `"/data"` is `{ view: "data" }`.
  - `"/settings/rules"` has `section: "rules"`; `"/settings/zzz"` is `{ view: "settings" }`.
  - `"/data/master/my%20table"` gives `table: "my table"`; `"/data/master/"` and `"//data//master"` do not throw; a malformed escape `"/data/%E0%A4%A"` returns the `notfound` route. (Review Focus 3)
  - `"/nope"` is `{ view: "notfound", path: "/nope" }`; `"/queue"` maps to `{ view: "activity" }` (old name).
  - Round trip: for each valid route sample, `parseRoute(buildPath(r))` equals `r`; `buildPath` percent-encodes segments.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement `lib/route.ts`.
- [ ] **Step 4: Write the failing tests** `lib/useRoute.test.tsx`: calling `navigate({ view: "settings" })` changes `location.pathname` to `/settings` and the returned route; `{ replace: true }` does not grow `history.length`; dispatching `popstate` after `history.back()` updates the route; navigating to the current path does not push a duplicate entry.
- [ ] **Step 5:** Implement `lib/useRoute.ts`. Create `app/[[...path]]/page.tsx` with the body of today's `app/page.tsx`; delete `app/page.tsx`. `/forbidden` keeps its own static page (static routes win over the catch-all).
- [ ] **Step 6:** Run `npm test && npm run typecheck && npm run build`. Expected: PASS, and the build output lists `/[[...path]]` and `/forbidden`.
- [ ] **Step 7:** Commit: `feat(ui): URL routes for every view`.

### Task 5: `useChanges` hook

**Files:**
- Create: `lib/useChanges.ts`, `lib/useChanges.test.tsx`

**Interfaces:**
- Consumes: `getQueue`, `decide`, `ApiError`, `Change`, `Me` from `@/lib/api`; `canDecide`, `upsert` from `@/lib/changes`; `connectEvents` from `@/lib/events`.
- Produces:
  ```ts
  export interface Changes {
    live: Change[];                 // newest known copy of every change seen
    mine: Change[];                 // pending changes this person can decide, oldest first
    online: boolean;
    deciding: number | null;        // id of the change a decision is in flight for
    error: string;
    schemaKey: number;              // bumps when a schema change is applied
    add(change: Change): void;      // a change proposed from this browser
    decide(change: Change, action: "approve" | "reject"): Promise<void>;
  }
  export function useChanges(me: Me, onEvent?: (change: Change, prev: Change | undefined) => void): Changes;
  export function activityLevel(pending: number): number; // 0 for 0, pending / 5 capped at 1
  ```
- `onEvent` is called for each WebSocket change with the previous known copy, so the shell can raise toasts without the hook knowing about them.

- [ ] **Step 1: Write the failing tests** (mock `@/lib/api` and `@/lib/events`): the queue is loaded on mount into `mine`; an event for a new pending change adds it and calls `onEvent(change, undefined)`; an event that turns a known pending change to `applied` removes it from `mine`, bumps `schemaKey` only when `kind === "schema"`, and calls `onEvent` with the old copy; `decide` sets `deciding` to the id while in flight and clears it; a rejected `decide` sets `error` to the `ApiError` message; `activityLevel(0) === 0`, `activityLevel(2) === 0.4`, `activityLevel(9) === 1`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement by moving the state and effects out of `AppShell.tsx` (the shell still uses its own copy until Task 7).
- [ ] **Step 4:** Run `npm test && npm run typecheck`. Expected: PASS.
- [ ] **Step 5:** Commit: `refactor(ui): useChanges hook owns live changes and decisions`.

### Task 6: Review tray

**Files:**
- Create: `components/ui/Tray.tsx`, `components/ReviewTray.tsx`, `components/ReviewTray.test.tsx`, `lib/shortcuts.ts`, `lib/shortcuts.test.ts`
- Modify: `components/ChangeCard.tsx`, `components/Components.test.tsx`, `components/ui/index.ts`, `app/ui.css`
- Delete: `components/ApprovalPopup.tsx`

**Interfaces:**
- Consumes: `Changes` from Task 5; `Badge`, `Button`, `EmptyState` from Task 2.
- Produces:
  - `Tray({ open, onClose, label, children })`: an `<aside role="complementary" aria-label={label}>`, rendered only while open; on open moves focus to its first focusable element, on close returns focus to the element focused before; `Esc` inside it calls `onClose`. It is not a modal: no backdrop, no focus trap, the page behind stays interactive.
  - `ReviewTray({ me, changes, open, onClose, onDecide, deciding })`, label `Needs review`, heading `Needs review`, a count `N waiting`, one `ChangeCard` per pending change oldest first, and an empty state titled `Nothing is waiting for review.` when the list is empty.
  - `isTyping(target: EventTarget | null): boolean` and `useShortcut(keys: string, handler: () => void)` in `lib/shortcuts.ts`. `keys` is either a chord like `"mod+k"` (Meta on macOS, Ctrl elsewhere; fires even while typing) or a sequence like `"g r"` (second key within 1000 ms; never fires while typing).
  - `ChangeCard` gains optional prop `deciding?: boolean` (disables only its own two buttons and shows `Applying…` on Approve); its `busy` prop stays as an alias. Its status badge renders through `Badge`, keeping the `badge` class and `data-status`.

- [ ] **Step 1: Write the failing tests** `lib/shortcuts.test.ts`: `isTyping` is true for `input`, `textarea`, `select` and a `contenteditable` element, false for a `button` and `null`; `"g r"` fires on `g` then `r`, not on `r` alone, not when the second key comes after 1000 ms, and not when the event target is a `textarea`; `"mod+k"` fires with `ctrlKey` or `metaKey` even when the target is a `textarea`. (Review Focus 2)
- [ ] **Step 2: Write the failing tests** `components/ReviewTray.test.tsx`: closed renders nothing; open shows a `complementary` region named `Needs review` and one card per pending change, oldest first; focus is inside the tray after opening and back on the opener after `Esc`; a control outside the tray can still be clicked while it is open; when `changes` re-renders with the open card now `applied`, the card is gone, the remaining card is still there and `document.activeElement` is still inside the tray (Review Focus 5); `deciding={7}` disables the buttons of card 7 only.
- [ ] **Step 3:** Run both. Expected: FAIL.
- [ ] **Step 4:** Implement. In `Components.test.tsx` replace the two `ApprovalPopup` tests ("shows nothing when there is nothing this person can decide", "asks about the oldest decidable change, counts the rest, and can snooze") with `ReviewTray` equivalents: empty shows `Nothing is waiting for review.`; with two decidable changes the oldest is first and the count reads `2 waiting`. The `Later` button and snoozing are removed by the spec (section 4).
- [ ] **Step 5:** Run `npm test && npm run typecheck`. Expected: PASS.
- [ ] **Step 6:** Commit: `feat(ui): review tray replaces the approval popup`.

### Task 7: The shell

**Files:**
- Create: `components/Rail.tsx`, `components/TopBar.tsx`, `components/NotFound.tsx`, `lib/nav.ts`, `lib/nav.test.ts`
- Modify: `components/AppShell.tsx`, `components/AppShell.test.tsx`, `components/DataView.tsx`, `components/GridEditor.tsx`, `components/GridEditor.test.tsx`, `components/QueueView.tsx`, `app/shell.css`

**Interfaces:**
- Consumes: `useRoute`, `Route`, `View` (Task 4); `useChanges`, `activityLevel` (Task 5); `ReviewTray`, `useShortcut` (Task 6); `ToastProvider`, `useToast` (Task 3); parts from Task 2.
- Produces:
  - `lib/nav.ts`: `NAV: { group: "Work" | "Quality" | "Review" | "System"; items: { view: View; label: string; icon: IconName; minRole: Role }[] }[]` and `navFor(role: Role)` (drops Scripts for a viewer; Activity stays for everyone). `breadcrumb(route: Route): string[]`.
  - `Rail({ me, route, navigate, waiting, online, onLogout })`: `<nav aria-label="Main">` with group headings, items as `<button aria-current="page">` when active, the Activity item's accessible name is `Activity` with a separate count badge `aria-label="N waiting"`. Foot: `Live` or `Reconnecting…`, email, role, `Sign out`.
  - `TopBar({ route, waiting, onOpenPalette, onOpenTray })`: breadcrumb, a button `Search or jump to` with `Kbd` hint, a `Review` button with the count.
  - `AppShell` keeps its signature `({ me, onLogout })` and now exposes the place through the URL. It wraps its content in `ToastProvider` and renders a `Skip to content` link, `Rail`, `TopBar`, `<main id="content">`, `ReviewTray`.

- [ ] **Step 1: Write the failing tests** `lib/nav.test.ts`: a viewer gets no Scripts item; an editor gets all eight; `breadcrumb({ view: "data", schema: "master", table: "province" })` is `["Data", "master", "province"]`.
- [ ] **Step 2: Extend `AppShell.test.tsx`** (keep the two existing tests; they must pass unchanged):
  - Clicking `Settings` sets `location.pathname` to `/settings` and marks the item `aria-current="page"`.
  - Rendering with `history.replaceState(null, "", "/duplicates")` first shows the Duplicates view.
  - Rendering at `/nope` shows `Page not found` and a `Go to Chat` button.
  - A WebSocket event for a new pending change this person can decide shows a toast `Change #9 needs your review` with a `Review` action, does not open the tray, and no element with `aria-modal="true"` exists. Clicking `Review` opens the tray.
  - No element with the text `Working…` is ever rendered.
  - A viewer sees no `Scripts` button.
- [ ] **Step 3:** Run. Expected: FAIL.
- [ ] **Step 4:** Implement `nav.ts`, `Rail`, `TopBar`, `NotFound`, and rewrite `AppShell` on top of `useChanges` and `useRoute`. Views receive the same props as today. Wiring rules: `onProposed` from a view calls `changes.add(c)` and, if `canDecide(me, c)`, opens the tray; the `onEvent` callback raises the toasts (pending and decidable by me, not requested by me from this browser: `Change #N needs your review`; a change of mine turning `applied`: success `Change #N applied`; `rejected` or `failed`: error `Change #N rejected` / `Change #N failed`); `online` going from false to true after the first connection: info `Reconnected`. `g r` opens the tray. Layout in `shell.css`: grid `208px minmax(0, 1fr)`; at `max-width: 1100px` the rail is 56px with labels as `title` tooltips and `sr-only` text; at `max-width: 720px` the rail is a fixed bottom bar showing Chat, Database, Data, Activity and a `More` menu, and `--shell-bottom: 56px` is set on `:root`; the tray is 320px wide and at `max-width: 720px` covers the full width.
- [ ] **Step 5:** Remove the full-screen overlay: delete the `busy` state's overlay markup and the `.overlay.busy`, `.busy-card`, `.busy-bar` rules. `DataView` keeps its `onBusy` prop optional and holds `loading` locally; `GridEditor` renders `<ProgressBar active={loading} label="Loading rows" />` above the table, sets `aria-busy` on `.grid-scroll` while loading, and shows `<Skeleton rows={8} />` instead of `Loading…` before the first page. In `GridEditor.test.tsx` add: while a page fetch is pending a `progressbar` named `Loading rows` is shown and the `Add row` button is still enabled. If an existing GridEditor test asserts the text `Loading…` for the first load, keep that text as `sr-only` inside the skeleton wrapper so it passes unchanged.
- [ ] **Step 6:** `QueueView` wraps its empty text in `EmptyState` (title `Nothing is waiting for review.`) and is shown for the `activity` route.
- [ ] **Step 7:** Run `npm test && npm run typecheck && npm run build`. Expected: PASS.
- [ ] **Step 8:** Commit: `feat(ui): side rail, top bar, URL navigation, no blocking overlay`.

### Task 8: Command palette

**Files:**
- Create: `lib/palette.ts`, `lib/palette.test.ts`, `components/ui/Palette.tsx`, `components/CommandPalette.tsx`, `components/CommandPalette.test.tsx`
- Modify: `components/AppShell.tsx`, `components/ui/index.ts`, `app/ui.css`

**Interfaces:**
- Consumes: `navFor` (Task 7), `Route`, `getSchemas`, `getGraph`, `useShortcut`.
- Produces:
  - `lib/palette.ts`: `interface Command { id: string; label: string; hint?: string; group: "Go to" | "Tables" | "Actions"; icon: IconName; run: (e: { shift: boolean }) => void }`; `match(query: string, label: string): number | null` (case-insensitive subsequence; lower is better: index of first matched character plus the span of the match; `""` matches everything with score 0); `rank(query: string, commands: Command[], limit = 12): Command[]` (stable for equal scores, group order Go to, Tables, Actions when the query is empty).
  - `Palette({ open, onClose, commands, placeholder })`: `role="dialog" aria-modal="true" aria-label="Command palette"`, an input with `role="combobox"`, results in `role="listbox"` with `role="option"`; Arrow keys move, `Enter` runs, `Shift+Enter` runs with `shift: true`, `Esc` closes; shows `No matches` when empty.
  - `CommandPalette({ me, open, onClose, navigate, refreshKey, onLogout })` builds the commands: one `Go to <label>` per nav item; one per table labelled `<schema>.<table>` (run navigates to `database`, with shift to `data`); actions `New chat`, `Sign out`, and for non-viewers `New table` (navigates to `database`), `Create connection link` (navigates to `connect`).

- [ ] **Step 1: Write the failing tests** `lib/palette.test.ts`: `match("prv", "province")` is a number and `match("xyz", "province")` is null; `match("pro", "province") < match("pro", "approve")`; matching ignores case; `rank("", cmds)` keeps group order; `rank` honours `limit`.
- [ ] **Step 2: Write the failing tests** `components/CommandPalette.test.tsx`: with `getSchemas` resolving one schema `master` and `getGraph` resolving two tables, typing `prov` lists `master.province` and `Enter` calls `navigate({ view: "database", schema: "master", table: "province" })`; `Shift+Enter` navigates to `data`; with `getSchemas` rejecting, `Go to Settings` and `New chat` are still listed and run (Review Focus 4); a viewer has no `New table`; `Esc` calls `onClose`; ArrowDown moves `aria-selected`.
- [ ] **Step 3:** Run. Expected: FAIL.
- [ ] **Step 4:** Implement. Schema data loads the first time the palette opens and again when `refreshKey` changes while it has been opened before. In `AppShell`, `useShortcut("mod+k", openPalette)` and the top bar button open it; add to `AppShell.test.tsx`: `Ctrl+K` pressed while focus is in the chat `textarea` opens the palette (Review Focus 2).
- [ ] **Step 5:** Run `npm test && npm run typecheck`. Expected: PASS.
- [ ] **Step 6:** Commit: `feat(ui): command palette`.

### Task 9: The black hole reflects system state

**Files:**
- Create: `lib/activity.ts`, `lib/activity.test.ts`
- Modify: `components/Cosmos.tsx`, `components/Cosmos.test.tsx`, `app/layout.tsx`, `components/AppShell.tsx`, `app/shell.css`

**Interfaces:**
- Consumes: `activityLevel` (Task 5).
- Produces: `lib/activity.ts`: `ACTIVITY_EVENT = "cosmos:activity"`, `FLARE_EVENT = "cosmos:flare"`, `setActivity(level: number): void` (clamps to 0..1, dispatches a `CustomEvent<number>`), `flare(): void`, `ease(current: number, target: number, dtSeconds: number, tau = 2): number` (exponential approach). `Cosmos` gains optional props `activity?: number` and `placement?: "hero" | "app"`; without the prop it follows the events. The root layout keeps rendering one `<Cosmos />`; the shell calls `setActivity(activityLevel(mine.length))` in an effect, `setActivity(0)` on unmount, and `flare()` when a change turns `applied`.

- [ ] **Step 1: Write the failing tests** `lib/activity.test.ts`: `ease(0, 1, 0)` is 0; `ease(0, 1, 2)` is about `0.632`; `ease` never overshoots; `setActivity(3)` dispatches detail `1`, `setActivity(-1)` detail `0`.
- [ ] **Step 2:** Add to `Cosmos.test.tsx`: at intensity `still`, dispatching `ACTIVITY_EVENT` schedules no animation frame; the existing six tests pass unchanged.
- [ ] **Step 3:** Run. Expected: FAIL.
- [ ] **Step 4:** Implement. In the draw loop keep an eased `a`; spin uses `SPIN * (1 + 0.6 * a)`, particle and halo alpha multiply by `(1 + 0.3 * a)`; a flare adds a 600 ms decaying boost to the photon ring's `shadowBlur` and stroke alpha. `body:has(.shell) .cosmos` opacity becomes `0.35`, and with the shell present the centre is `cx = w * 0.82, cy = h * 0.78` (hero placement stays `0.72 / 0.42`); the shell sets `data-app` on `<html>` so the canvas can read placement without a prop from the layout.
- [ ] **Step 5:** Run `npm test && npm run typecheck`. Expected: PASS.
- [ ] **Step 6:** Commit: `feat(ui): the disk spins up while changes wait`.

### Task 10: Sign-in, loading and error screens

**Files:**
- Modify: `components/Home.tsx`, `components/Home.test.tsx`, `components/HomeClient.tsx`, `app/error.tsx`, `app/forbidden/page.tsx`, `lib/i18n.ts`, `app/shell.css`

**Interfaces:**
- Produces: `Home({ locale, loginUrl, loading? })` (signed-out only; `me` and `onLogout` props removed). New i18n key `signInGoogle` (`Sign in with Google` / `เข้าสู่ระบบด้วย Google`) in both locales.

- [ ] **Step 1: Update `Home.test.tsx`:** keep "shows a Google sign-in link when signed out" (the link's name is now `Sign in with Google`, its `href` is `loginUrl`), "renders Thai text" and "has the same keys in English and Thai". Remove the four tests of the signed-in abilities list ("viewer can only read", "editor can read and edit", "approver can do everything", "shows sign-out only when a handler is given"): that render is unreachable (spec section 9). Add: `loading` renders the title and tagline, no sign-in link, and a `status` region reading `Loading…`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Layout: two columns at 900px and wider (text left, the hole shows through on the right), one column below. `HomeClient` renders `<Home loading />` until `getMe` settles. `app/error.tsx`: `Panel` with title `Something went wrong`, the error's `digest` if present, and a `Reload` button calling `reset()`. `forbidden/page.tsx` takes the same panel look; its copy is unchanged.
- [ ] **Step 4:** Run `npm test && npm run typecheck && npm run build`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(ui): sign-in, loading and error screens`.

### Task 11: Verify in a browser and open the pull request

**Files:**
- Modify: `README.md` (section "Look and feel": tokens, tray, palette, routes, the state-aware disk; remove the sentence that describes the approval popup as a modal)
- Create: `docs/superpowers/screens/2026-11-10/` (screenshots)

- [ ] **Step 1:** Run `npm run build && npm start`. In the system temp directory (not in the repo) write a throwaway Node script that listens on one port, proxies everything to port 3000 except `/api/*`, and answers `/api/me`, `/api/queue`, `/api/schema/schemas`, `/api/schema/graph`, `/api/schema/tables/province/rows`, `/api/chat/conversations`, `/api/settings`, `/api/rules` with fixtures shaped like the types in `lib/api.ts` (one pending schema change in the queue; Thai province rows).
- [ ] **Step 2:** With Playwright (Chromium), capture at 1440, 1024 and 390 px wide: sign-in, chat, database, data grid, activity with the tray open, settings, the palette open. Repeat the 1440 set with `reducedMotion: "reduce"`. Save under `docs/superpowers/screens/2026-11-10/`.
- [ ] **Step 3:** Look at every screenshot. Fix what is wrong (overlap, clipped text, unreadable contrast over the disk, a bottom bar covering the grid footer at 390 px). Re-capture. Check the browser console is free of errors and hydration warnings on each page.
- [ ] **Step 4:** Run `npm test && npm run typecheck && npm run build` and `grep -rnE "#[0-9a-fA-F]{6}\b" app/*.css | grep -v tokens.css`. Expected: tests pass; grep prints nothing.
- [ ] **Step 5:** Commit `docs: README and screenshots for the new shell`, push the branch, open a pull request against `main` titled `UI rework 1: shell and design system`. The body lists: what changed, every existing test that was changed or removed and why (Task 6: two popup tests; Task 10: four abilities tests), the screenshots, and what piece 2 will do.
