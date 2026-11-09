# Labels toggle for editable FK cells — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make "show labels" also affect FK cells rendered as `<select>`, which is the only way they render for anyone with edit rights — so the toggle stops appearing to do nothing.

**Architecture:** `displayText()` (GridEditor.tsx ~line 142) resolves an id to a label, but it is only applied to read-only `<span>` cells (line ~145). Editable FK cells go through `input()` (line 114) which returns a `<select>` and never consults `displayText`. Fix at the `<select>` branch: keep real `<option value=id>` entries for editing, but show the resolved label as the selected option's text when labels mode is on.

**Tech Stack:** Next.js 15, React, TypeScript, Vitest + @testing-library/react.

**Spec:** `docs/superpowers/specs/2026-11-08-five-ui-gaps-design.md` §5

## Global Constraints

- No new dependencies.
- Editing must keep working — do NOT force read-only. Option (B) in the spec is explicitly rejected.
- The existing test only covers role `viewer`, which is exactly why this shipped broken. Any new test must use an editable role.
- `cd frontend && npm test` (136 existing) and `npm run typecheck` stay green at every commit.

---

### Task 1: Apply resolved labels inside the FK `<select>`

**Files:**
- Modify: `frontend/components/GridEditor.tsx:114-125` (the `input()` FK branch)
- Test: `frontend/components/GridEditor.test.tsx`

**Interfaces:**
- Consumes: `displayText(c, text)` (existing, ~line 142) and the `refs` map
- Produces: labels visible for editable sessions

Current FK branch (lines 117-125):

```tsx
    const ref = c.references ? refs[c.references.table] : undefined;
    if (ref) {
      return (
        <select {...common} value={value} onChange={(e) => setCell(key, c.name, e.target.value)}>
          <option value="">{c.nullable ? "—" : "choose…"}</option>
          {ref.options.map((o) => <option key={String(o.id)} value={String(o.id)}>{o.label}</option>)}
          {value && !ref.options.some((o) => String(o.id) === value) && <option value={value}>#{value}</option>}
        </select>
      );
    }
```

- [ ] **Step 1: Write the failing test (editable role)**

Add to `frontend/components/GridEditor.test.tsx`. Read the existing `setup("viewer", ...)` helper and its `ROWS`/option mocks first — the mock returns labels `"Acme"`/`"Beta"`; reuse those exact values.

```tsx
  it("shows the FK label in an editable cell too", async () => {
    await setup("editor", page(ROWS));
    // editor => FK renders as <select>; the selected option must read the label
    const sel = screen.getByLabelText(/customerId/i) as HTMLSelectElement;
    expect(sel.tagName).toBe("SELECT");
    expect(sel.options[sel.selectedIndex].textContent).toBe("Acme");
  });
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run components/GridEditor.test.tsx -t "editable cell"`
Expected: FAIL — `selectedIndex` text is the raw id (e.g. `"10"`), not `"Acme"`.

- [ ] **Step 3: Implement**

In the FK `<select>` branch, make the option text label-aware. Replace the two `<option>` lines with:

```tsx
          {ref.options.map((o) => <option key={String(o.id)} value={String(o.id)}>{o.label}</option>)}
          {value && !ref.options.some((o) => String(o.id) === value) && (
            <option value={value}>{displayText(c, value)}</option>
          )}
```

and, critically, ensure the **currently selected** known option also shows its label by giving the `<select>` a resolved display when labels are on. Add just above the `return (`:

```tsx
    if (ref) {
      // ponytail: labels mode drives display text; option VALUES stay ids so editing still saves the FK
      const shown = displayText(c, value);
      return (
        <select {...common} value={value} title={shown !== value ? value : undefined} onChange={(e) => setCell(key, c.name, e.target.value)}>
          <option value="">{c.nullable ? "—" : "choose…"}</option>
          {ref.options.map((o) => (
            <option key={String(o.id)} value={String(o.id)}>{o.label}</option>
          ))}
          {value && !ref.options.some((o) => String(o.id) === value) && <option value={value}>{shown}</option>}
        </select>
      );
    }
```

`title` exposes the true id on hover so a resolved label is never confused for stored data.

- [ ] **Step 4: Run the tests**

Run: `cd frontend && npm test`
Expected: PASS, 137+ tests.

- [ ] **Step 5: Verify load-bearing**

Swap `displayText(c, value)` for plain `value` in that branch, confirm the new test FAILS, restore. Mandatory.

- [ ] **Step 6: Commit**

```bash
git add frontend/components/GridEditor.tsx frontend/components/GridEditor.test.tsx
git commit -m "fix(ui): resolve FK labels in editable cells too"
```

---

### Task 2: Toggle-off must show the raw id

**Files:**
- Modify: `frontend/components/GridEditor.tsx` (same branch)
- Test: `frontend/components/GridEditor.test.tsx`

**Interfaces:**
- Consumes: the `showRaw` state added earlier (`show labels` checkbox)

- [ ] **Step 1: Write the failing test**

```tsx
  it("shows the raw id when labels are toggled off", async () => {
    await setup("editor", page(ROWS));
    const sel = screen.getByLabelText(/customerId/i) as HTMLSelectElement;
    expect(sel.options[sel.selectedIndex].textContent).toBe("Acme");
    fireEvent.click(screen.getByRole("checkbox", { name: /show labels/i }));
    await waitFor(() => {
      const s = screen.getByLabelText(/customerId/i) as HTMLSelectElement;
      expect(s.options[s.selectedIndex].textContent).toMatch(/^#?10$/);
    });
  });
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run components/GridEditor.test.tsx -t "raw id when labels"`
Expected: FAIL — toggling changes nothing.

- [ ] **Step 3: Implement**

`displayText` already short-circuits on `showRaw` (line 142: `if (showRaw || ...) return text;`). Wire the FK branch so the **selected** option's text comes from `displayText` in both directions; keep every other option's label intact so the dropdown stays browsable. If the "show labels" checkbox is currently unchecked-by-default, leave that default as-is.

- [ ] **Step 4: Run the tests**

Run: `cd frontend && npm test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/components/GridEditor.tsx frontend/components/GridEditor.test.tsx
git commit -m "fix(ui): labels toggle affects both directions"
```

---

### Task 3: Real-browser verification on prod

- [ ] **Step 1:** Deploy `web` per the deployment skill.
- [ ] **Step 2:** Mint a Django session as the seeded admin (references/real-browser-prod-verification.md), open Data → `subdistrict` at 1440px.
- [ ] **Step 3:** Confirm the `district_id` cell reads a district **name** with labels on, and an id with labels off. This is the exact cell that rendered a Thai district `<select>` in the investigation.
- [ ] **Step 4:** Confirm editing still saves — change one FK, propose, and see the change queue it (then delete the smoke row so prod tables return to 0).

Skipped: labels for boolean columns (already `true`/`false`, no id involved). Add never, unless requested.
