# Rows-per-page + loading on page change — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Data-tab pagination show the existing loading overlay and let the user choose rows per page instead of the hardcoded 50.

**Architecture:** `busy` already lives in AppShell (`AppShell.tsx:24`) but is threaded only into ChatView/QueueView — DataView and GridEditor never receive it. Thread `setBusy` down AppShell → DataView → GridEditor and wrap the existing `getRows` effect. Page size becomes a `useState` replacing the `PAGE` const; the API already accepts `limit`, so there is no backend change.

**Tech Stack:** Next.js 15 App Router, React hooks, TypeScript, Vitest + @testing-library/react.

**Spec:** `docs/superpowers/specs/2026-11-08-five-ui-gaps-design.md` §2

## Global Constraints

- No new dependencies (ponytail ladder: reuse what's installed).
- Every step ends green: `cd frontend && npm test` (136 existing tests must keep passing) and `npm run typecheck`.
- Root cause fix, once: set busy at the single fetch site, not per-button.
- Tests must be load-bearing — verify a test fails when the fix is reverted.

---

### Task 1: Thread `setBusy` from AppShell into GridEditor

**Files:**
- Modify: `frontend/components/AppShell.tsx:90-91` (DataView mount)
- Modify: `frontend/components/DataView.tsx:5,41`
- Modify: `frontend/components/GridEditor.tsx:16-25` (Props)

**Interfaces:**
- Consumes: `AppShell.tsx:24` `const [busy, setBusy] = useState(false);`
- Produces: `GridEditor` gains prop `onBusy: (b: boolean) => void`

- [ ] **Step 1: Add the prop to GridEditor's Props and signature**

In `frontend/components/GridEditor.tsx`, extend the `Props` interface (line ~16) with:

```ts
  /** Set true while a page fetch is in flight; drives the overlay in AppShell. */
  onBusy: (b: boolean) => void;
```

and change the signature at line 25:

```ts
export function GridEditor({ me, table, live, onProposed, onBusy }: Props) {
```

- [ ] **Step 2: Thread it through DataView**

In `frontend/components/DataView.tsx`, add `onBusy: (b: boolean) => void;` to its props interface and pass it at line 41:

```tsx
{table && <GridEditor me={me} table={table} live={live} onProposed={onProposed} onBusy={onBusy} />}
```

Also add `onBusy` to DataView's own props interface and destructuring.

- [ ] **Step 3: Pass `setBusy` where DataView is mounted**

In `frontend/components/AppShell.tsx` at the DataView mount (~line 90):

```tsx
<DataView me={me} live={live} refreshKey={schemaKey} onBusy={setBusy}
```

(keep the existing props already on that element — add `onBusy={setBusy}` only).

- [ ] **Step 4: Typecheck**

Run: `cd frontend && npm run typecheck`
Expected: PASS (DataView/GridEditor callers now satisfy the new required prop).

- [ ] **Step 5: Commit**

```bash
git add frontend/components/AppShell.tsx frontend/components/DataView.tsx frontend/components/GridEditor.tsx
git commit -m "refactor(ui): thread setBusy into GridEditor"
```

---

### Task 2: Wrap the fetch effect so every page change shows the overlay

**Files:**
- Modify: `frontend/components/GridEditor.tsx:59` (getRows effect)

**Interfaces:**
- Consumes: `onBusy` prop from Task 1; the existing `getRows` effect at line 59
- Produces: overlay visible during fetch

The effect currently reads (line ~59):

```ts
    getRows(table, { offset, limit: PAGE, sort, dir, filters: applied, inactive })
```

- [ ] **Step 1: Write the failing test**

Add to `frontend/components/GridEditor.test.tsx` (read the existing file first and follow its `setup()` helper):

```tsx
  it("reports busy while a page is loading", async () => {
    const busy: boolean[] = [];
    await setup("editor", page(ROWS), (b) => busy.push(b));
    expect(busy).toContain(true);
  });
```

Extend the existing `setup()` helper to accept and forward an `onBusy` spy — do not duplicate the mock wiring.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npx vitest run components/GridEditor.test.tsx -t "reports busy"`
Expected: FAIL — `busy` stays empty (nothing calls it yet).

- [ ] **Step 3: Wrap the fetch**

Change the effect so it becomes:

```ts
    setBusy... // NO — call the prop: onBusy(true)
    getRows(table, { offset, limit: PAGE, sort, dir, filters: applied, inactive })
      .then(setPage)
      .catch(...)
      .finally(() => onBusy(false));
```

Concretely: surround the existing `getRows(...)` call in the `useEffect` with
`onBusy(true)` before it and `onBusy(false)` in a `.finally(...)` on the promise
chain. Keep the existing `.then`/`.catch` behaviour untouched.

- [ ] **Step 4: Run the tests**

Run: `cd frontend && npm test`
Expected: PASS, 137+ tests.

- [ ] **Step 5: Verify the test is load-bearing**

Revert only the `onBusy(true)` line, re-run the single test, confirm it FAILS, then restore it. A test that passes either way is worthless — this step is mandatory.

- [ ] **Step 6: Commit**

```bash
git add frontend/components/GridEditor.tsx frontend/components/GridEditor.test.tsx
git commit -m "fix(ui): show the loading overlay when the Data tab fetches"
```

---

### Task 3: Selectable rows per page

**Files:**
- Modify: `frontend/components/GridEditor.tsx:14` and the fetch call
- Test: `frontend/components/GridEditor.test.tsx`

**Interfaces:**
- Consumes: nothing new
- Produces: `PAGE_SIZES` const, `pageSize` state

- [ ] **Step 1: Write the failing test**

```tsx
  it("fetches with the chosen page size", async () => {
    const { getRows } = await setup("viewer", page(ROWS));
    fireEvent.change(screen.getByLabelText("Rows per page"), { target: { value: "100" } });
    await waitFor(() => expect(getRows).toHaveBeenCalledWith(
      expect.anything(), expect.objectContaining({ limit: 100 })));
  });
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run components/GridEditor.test.tsx -t "chosen page size"`
Expected: FAIL — no such control exists.

- [ ] **Step 3: Implement**

Replace line 14 `const PAGE = 50;` with:

```ts
const PAGE_SIZES = [25, 50, 100, 200] as const;
```

and inside the component add state:

```ts
  const [pageSize, setPageSize] = useState(50);
```

Use `pageSize` in the `getRows` call instead of `PAGE`, add `pageSize` to that
effect's dependency array, and reset `offset` to 0 when the size changes.
Render the control next to the existing "show inactive" label, following that
label's markup:

```tsx
        <label className="small">
          Rows per page{" "}
          <select aria-label="Rows per page" value={pageSize}
            onChange={(e) => { setPageSize(Number(e.target.value)); setOffset(0); }}>
            {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
```

- [ ] **Step 4: Run the tests**

Run: `cd frontend && npm test`
Expected: PASS.

- [ ] **Step 5: Verify load-bearing, then commit**

Revert `limit: pageSize` back to a literal `50`, confirm the new test FAILS, restore, then:

```bash
git add frontend/components/GridEditor.tsx frontend/components/GridEditor.test.tsx
git commit -m "feat(ui): choose rows per page in the Data tab"
```

---

### Task 4: Real-browser check on prod

Do not claim success from green tests alone — the owner requires live verification.

- [ ] **Step 1:** Deploy per `devops/project-singularity-deployment` (build web, up -d web).
- [ ] **Step 2:** Mint a Django session (see `references/real-browser-prod-verification.md`), open the Data tab at 1440px, select `subdistrict`.
- [ ] **Step 3:** Click Next and confirm the overlay (`.overlay.busy`) appears; throttle the network if the fetch is too fast to observe.
- [ ] **Step 4:** Change "Rows per page" to 100 and confirm the request goes out with `limit=100`.

Skipped: server-side pagination caps. Add when someone reports a page-size DoS.
