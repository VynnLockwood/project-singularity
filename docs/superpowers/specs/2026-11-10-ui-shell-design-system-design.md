# UI rework, piece 1: shell and design system

Date: 2026-11-10. Status: design approved by the product owner in chat; this file is for written review.
Base: `origin/main` at c334eaa (PRs 1 to 21 merged).

## 0. Summary

The owner asked for a full sweep of Singularity with the UI first. The sweep is four pieces, each with its own spec, plan and pull request:

1. **Shell and design system** (this file).
2. View rework: Chat, Data grid, Database and ERD, Activity (queue, history, audit), Settings, Scripts, Connect, Duplicates.
3. Backend additions the UI needs: chat streaming, home stats, deeper health check.
4. Backend health: real worker with nightly backup, security headers, stray root files, stale README lines.

Piece 1 replaces the frame every view sits in and the styles every view uses. No view loses a feature. The backend is not touched.

### Decisions taken (from the owner)

| Question | Decision |
|---|---|
| Scope | Full sweep, UI first. |
| Theme | Keep the black hole with an orange accretion disk. |
| Status colour | Taken from the disk: blue-white for applied, orange for waiting, deep red for rejected or failed. |
| Approvals | A side tray replaces the modal that takes over the screen. |
| Loading | No full-screen overlay. The part that loads shows its own progress. |
| Type | Chakra Petch for titles, IBM Plex Sans and IBM Plex Sans Thai for text, IBM Plex Mono for SQL and identifiers. |

### Assumptions (change me if wrong)

- Desktop is the main target; the app must stay usable on a phone (as today).
- English UI only for now. `lib/i18n.ts` stays as it is.
- The self-approval rule is unchanged: an editor still approves their own data change, now from the tray.
- No new UI component library. The project has four runtime dependencies and stays near that.
- Work lands as a pull request against `main`, authored as the owner, like PRs 1 to 21.

## 1. Design tokens

All colour, type, spacing and motion values become CSS custom properties in `app/tokens.css`. No component or stylesheet outside that file contains a hex value. `app/globals.css` is split by concern (section 6).

### 1.1 Colour

| Token | Value | Use |
|---|---|---|
| `--void` | `#050201` | Page background |
| `--surface-1` | `#0d0603` | Rail, tray, panels |
| `--surface-2` | `#160a05` | Cards, inputs, table header |
| `--surface-3` | `#22110a` | Hover, selected row, popovers |
| `--line` | `#2a150a` | Hairlines |
| `--line-strong` | `#5a2c12` | Input and card borders |
| `--text` | `#fbe9d0` | Body text |
| `--text-bright` | `#ffe2b0` | Titles, selected items |
| `--text-dim` | `#b8926b` | Supporting text |
| `--text-faint` | `#8a6a4c` | Captions, placeholders |
| `--disk` | `#ff9d2e` | Accent, primary action, waiting |
| `--disk-hot` | `#ffd479` | Unsaved edits, warnings |
| `--blueshift` | `#9fd0ff` | Applied, added, success |
| `--redshift` | `#ff7a5c` | Rejected, failed, removed, errors |

Each status colour has a matching tint for backgrounds (`--blueshift-tint` `#0b1a26`, `--redshift-tint` `#2a0d08`, `--disk-tint` `#3a2408`). Text on `--disk` fills uses `--on-disk` `#2a1203`.

Contrast: every text and background pair in the table is checked to WCAG AA (4.5:1 for text, 3:1 for large text and UI marks) by a unit test that reads `tokens.css`. A pair that fails is adjusted in the token file, not in a component.

Status is never colour alone. Every status shows an icon and a word next to the colour.

### 1.2 Type

Fonts are self-hosted through `@fontsource` packages, so `docker build` needs no font server and the internal network needs no internet.

| Role | Family | Sizes |
|---|---|---|
| Title | Chakra Petch 600 | 28 / 20 / 16 px |
| Text | IBM Plex Sans 400 and 500, IBM Plex Sans Thai as fallback for Thai glyphs | 14 px base, 12 px small |
| Code | IBM Plex Mono 400 | 13 px |

Line length for prose (chat, help text) is capped at 72 characters. Tables are exempt.

### 1.3 Space, radius, motion

Spacing scale 4, 8, 12, 16, 24, 32 px. Radius 6 px for controls, 10 px for panels; pills only for badges. Motion: 120 ms for hover and press, 200 ms for tray and palette. All motion is removed under `prefers-reduced-motion: reduce`.

## 2. Shell layout

```
+----------+--------------------------------------------+-----------+
| rail     | top bar: breadcrumb          search  ⌘K    |           |
| 208 px   +--------------------------------------------+  review   |
|          |                                            |  tray     |
| Work     |  view                                      |  320 px   |
| Quality  |                                            |  (closed  |
| Review   |                                            |  by       |
| System   |                                            |  default) |
| -------- |                                            |           |
| live, me |                                            |           |
+----------+--------------------------------------------+-----------+
```

- **Rail.** Groups: Work (Chat, Database, Data), Quality (Duplicates, Scripts), Review (Activity, with the count of changes waiting for this person), System (Connect, Settings). Role rules are the ones in `AppShell` today: a viewer does not see Scripts, and Activity shows history only. The foot of the rail shows live or reconnecting, the person's email and role, and Sign out. Below 1100 px the rail shows icons with tooltips; below 720 px it becomes a bottom bar with the five most used items and a More menu.
- **Top bar.** Breadcrumb for the current place (`master / province`) and the search field that opens the command palette.
- **Review tray.** Section 4.
- The `<nav aria-label="Main">` landmark and the tab names stay, so existing tests and screen reader users find the same items.

In piece 1 the Activity item shows what the Queue tab shows today. The history and audit views arrive in piece 2.

## 3. URL state

The place is held in the URL so refresh, back, forward and shared links work.

| Path | View |
|---|---|
| `/` | Chat, new conversation |
| `/chat/<id>` | Chat, that conversation |
| `/database/<schema>` and `/database/<schema>/<table>` | Database |
| `/data/<schema>/<table>` | Data grid |
| `/duplicates`, `/scripts`, `/activity`, `/connect`, `/settings` | Those views |
| `/settings/<section>` | Settings section |

Implementation: one optional catch-all route, `app/[[...path]]/page.tsx`, renders the same client app. A small `lib/route.ts` parses a path into a typed `Route` and builds a path from one; it is pure and unit tested both ways. Views receive their part of the route as props and call `navigate(route)`; they do not read the URL themselves. An unknown path shows a "Page not found" panel with a link to Chat. `/forbidden` keeps its own page.

In piece 1 the deep parts that views do not yet accept as props (conversation id, schema and table) are parsed and kept in the URL but each view adopts them in piece 2. Top-level navigation works fully in piece 1.

## 4. Review tray

Replaces `ApprovalPopup`.

- Closed by default. The Activity count in the rail and a button in the top bar show how many changes wait for this person.
- When a new change arrives for this person: a toast ("Change #214 needs your review", with a Review action) and one pulse of the count. Nothing covers the screen.
- **Exception:** a change this person has just proposed from the web (grid save, a form in Database, a chat turn) opens the tray at once, because approving it is the next step of what they were doing.
- The tray lists the pending changes, oldest first, each as the existing `ChangeCard`. Approve and Reject work as today and show progress on the card itself.
- "Later" is gone; closing the tray does the same job and forgets nothing.
- Keyboard: `g r` opens the tray, `Esc` closes it, focus moves into the tray on open and returns on close. The tray is a `complementary` landmark, not a modal dialog, so the page behind stays usable.

## 5. Feedback: loading, toasts, empty and error states

- **Loading.** The `busy` overlay in `AppShell` is removed. `onBusy` from the grid drives a 2 px progress bar at the top of the grid and dims only the rows. A first load shows skeleton rows. A decision in progress disables only the two buttons on that card.
- **Toasts.** A `ToastProvider` with `toast.success`, `toast.error` and `toast.info`. Bottom right, at most three, `role="status"` (errors `role="alert"`), auto-dismiss after 5 s except errors. Used for: saved, applied, rejected, failed, copied, reconnected. Inline errors next to a field stay inline.
- **Empty states.** Each view's empty text becomes a small panel: what this place is, and one action. Wording keeps the sentences tests already assert.
- **Errors.** `app/error.tsx` gets the new look and a "Reload" action. The shell-level error line stays as an alert region.

## 6. Shared parts and file layout

New folder `components/ui/`. Each part is one small file with its own test.

| Part | Job |
|---|---|
| `Button` | Variants `primary`, `secondary`, `ghost`, `danger`; sizes `sm`, `md`. Renders a `<button>` or, with `href`, an `<a>`. |
| `Badge` | Status badge with icon and word; one place maps a change status to colour. |
| `Panel` | Bordered surface with optional title and actions. |
| `Icon` | Inline SVG from a small local set (about 24 icons), `aria-hidden` by default. |
| `Toast` | Provider, hook, viewport. |
| `Tray` | The slide-in side region used by the review tray. |
| `Palette` | Command palette (section 7). |
| `Skeleton`, `ProgressBar`, `EmptyState`, `Kbd` | As named. |

Existing class names (`button`, `link`, `tab`, `badge`, `muted`, `problems`, and the rest) keep working: `globals.css` maps them onto the tokens, so every view takes the new look in piece 1 without being rewritten. Views move to the `ui/` parts one by one in piece 2.

Styles: `app/tokens.css`, `app/base.css` (reset, type, form controls, focus ring), `app/shell.css`, `app/legacy.css` (the old class names, now token based), and `app/ui.css` for the `ui/` parts (plain CSS with `ui-` prefixed classes; CSS modules are avoided because the test runner does not resolve their class names).

`AppShell.tsx` is split: `AppShell` (layout and wiring), `Rail`, `TopBar`, `ReviewTray`, and a `useChanges` hook that owns the live list, the WebSocket and `onDecide`.

## 7. Command palette

Opens with `⌘K` or `Ctrl+K`, or from the search field. A `dialog` with a text input and a list (`combobox` and `listbox` roles, arrow keys, Enter, Esc).

Sources, in this order: views ("Go to Settings"), databases and tables (from `getSchemas` and `getGraph`, loaded when the palette first opens and refreshed on a schema change event), and actions the role allows ("New chat", "New table", "Create connection link", "Sign out"). Matching is a case-insensitive subsequence match on the label, ranked by match position; it is a pure function with unit tests. Picking a table goes to `/database/<schema>/<table>`; with `Shift+Enter` it goes to `/data/<schema>/<table>`.

Schema search by meaning stays where it is (Database overview). The palette does not call `/api/search`.

## 8. The black hole reflects system state

`Cosmos` gains one input, `activity`, a number from 0 to 1 derived in the shell from the count of pending changes this person can see (0 for none, rising to 1 at five or more). It scales the disk's spin and brightness between the current look (0) and about 1.6 times spin and 1.3 times brightness (1), eased over two seconds so it never jumps. When a change is applied, one short brightening of the inner ring plays.

Rules that do not change: `prefers-reduced-motion` forces `still`; the five intensity levels in Settings still set the particle budget; the particles still come from the Rust engine and its TypeScript twin, whose golden tests are untouched because only the draw step reads `activity`.

Inside the app the canvas sits behind the main area at 35% opacity, with its centre moved to the lower right so it does not sit under text. On the sign-in screen it is at full strength.

## 9. Sign-in screen

The hole at full strength on the right, and on the left: the name in Chakra Petch, the line "Design master data with a prompt.", one button "Sign in with Google", and a short line saying who can sign in (domains come from the server; if not available, the line is left out). The loading state before `getMe` resolves is the same screen without the button, so there is no flash and no layout jump. The signed-in "abilities" list in `Home.tsx` is no longer reachable (a signed-in person always gets the shell); `Home` keeps the signed-out render only, and its tests are updated to match.

## 10. Accessibility

- Visible focus ring on every control (2 px `--disk`, 2 px offset).
- A "Skip to content" link as the first focusable element.
- Landmarks: `nav` (Main), `main`, `complementary` (review tray), `status` regions for toasts.
- All new parts are usable with a keyboard alone; each has a test for its keys.
- Hit targets at least 32 px on desktop, 44 px in the phone bottom bar.

## 11. Testing

- Existing suites (`npm test`, `npm run typecheck`, `npm run build`) pass. A test changes only where the thing it asserts was deliberately changed by this spec; each such change is named in the pull request.
- New unit tests: `route.ts` (parse and build, round trip, unknown paths), palette matching, toast queue, tray focus handling, token contrast, `useChanges`, `activity` mapping.
- `AppShell.test.tsx` is extended: navigation updates the URL; a deep link opens the right view; a new pending change shows a toast and does not cover the page; a change proposed from the grid opens the tray.
- Manual check in a real browser at 1440, 1024 and 390 px wide, with reduced motion on and off, before the pull request is opened. Screenshots go in the pull request.

## 12. Out of scope for piece 1

Reworking the inside of any view; chat streaming and markdown; the Activity history and audit views; the Data tab's database picker; any backend change; a light theme; Thai UI text.
