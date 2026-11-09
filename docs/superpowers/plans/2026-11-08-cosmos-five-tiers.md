# Five animation tiers + rename — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the background from 3 intensity levels to 5 with clearer names, and migrate any user already storing an old level name.

**Architecture:** All tiers are one const map, `LEVELS` in `frontend/lib/cosmos.ts`. `readIntensity()` validates `localStorage` against `LEVELS` and falls back to `intense` on any unknown value — so renaming the keys without a migration silently resets everyone to the default. The rename therefore needs an explicit legacy mapping in `readIntensity()`. The Settings select enumerates `Object.keys(LEVELS)`, so it picks up new tiers automatically.

**Tech Stack:** Next.js 15, TypeScript, Vitest, canvas (rAF) via existing Cosmos scene.

**Spec:** `docs/superpowers/specs/2026-11-08-five-ui-gaps-design.md` §3

## Global Constraints

- No new dependencies.
- The localStorage key is `singularity.cosmos_intensity` — a stale value must migrate, never silently reset.
- This is GPU-bound on the client: keep counts tunable constants, cap the top tier conservatively.
- `cd frontend && npm test` (136 existing) and `npm run typecheck` green at every commit.

---

### Task 1: Extend LEVELS to five tiers and rename

**Files:**
- Modify: `frontend/lib/cosmos.ts:7-14`
- Modify: `frontend/components/SettingsView.tsx:18` (default value `"intense"`)
- Test: `frontend/components/Cosmos.test.tsx`

**Interfaces:**
- Consumes: nothing
- Produces: `LEVELS` with 5 entries; `Intensity` type widens automatically

Current map (cosmos.ts:8-12):

```ts
export const LEVELS = {
  reduced: { particles: 0,    stars: 160, streak: 0,   glow: 0 },
  calm:    { particles: 700,  stars: 160, streak: 1,   glow: 1 },
  intense: { particles: 3200, stars: 320, streak: 2.2, glow: 2 },
} as const;
```

- [ ] **Step 1: Write the failing test**

Add to `frontend/components/Cosmos.test.tsx` (read the existing file; it has cases for `reduced` and `intense` already):

```tsx
  it("offers five tiers including two above steady", async () => {
    expect(Object.keys(LEVELS)).toEqual(["still", "subtle", "steady", "vivid", "supernova"]);
  });
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run components/Cosmos.test.tsx -t "five tiers"`
Expected: FAIL — keys are `["reduced","calm","intense"]`.

- [ ] **Step 3: Implement the new map**

Replace the `LEVELS` const with:

```ts
export const LEVELS = {
  still:     { particles: 0,     stars: 160, streak: 0,   glow: 0 },
  subtle:    { particles: 700,   stars: 160, streak: 1,   glow: 1 },
  steady:    { particles: 3200,  stars: 320, streak: 2.2, glow: 2 },
  vivid:     { particles: 6000,  stars: 480, streak: 3.0, glow: 3 },
  supernova: { particles: 10000, stars: 700, streak: 4.0, glow: 4 },
} as const;
```

Update the doc comment above it to describe five tiers. The two top counts are
starting estimates — tune by eye, they are deliberately conservative because
they are GPU-bound (`# ponytail: particle counts are hand-tuned, raise only after measuring on a low-end device`).

- [ ] **Step 4: Run the tests**

Run: `cd frontend && npm test`
Expected: PASS (other Cosmos cases may need their level names updated — see Task 2).

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/cosmos.ts frontend/components/Cosmos.test.tsx
git commit -m "feat(ui): five background tiers, renamed"
```

---

### Task 2: Migrate legacy stored intensities

**Files:**
- Modify: `frontend/lib/cosmos.ts:17-23` (`readIntensity`)
- Modify: `frontend/components/SettingsView.tsx:18,90`
- Test: `frontend/components/Cosmos.test.tsx`

**Interfaces:**
- Consumes: localStorage key `singularity.cosmos_intensity`
- Produces: `readIntensity()` returning a valid new-style `Intensity`

Current code (cosmos.ts:20-23):

```ts
export function readIntensity(): Intensity {
  const v = typeof localStorage === "undefined" ? null : localStorage.getItem(KEY);
  return v && v in LEVELS ? (v as Intensity) : "intense";
}
```

- [ ] **Step 1: Write the failing test**

```tsx
  it("migrates a stored legacy level instead of resetting it", async () => {
    localStorage.setItem("singularity.cosmos_intensity", "calm");
    expect(readIntensity()).toBe("subtle");
  });
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run components/Cosmos.test.tsx -t "legacy level"`
Expected: FAIL — returns `"steady"` (the fallback), not `"subtle"`.

- [ ] **Step 3: Implement the mapping**

Add above `readIntensity`:

```ts
/** Old names shipped before the five-tier rename; map them so nobody loses their pick. */
const LEGACY: Record<string, Intensity> = { reduced: "still", calm: "subtle", intense: "steady" };
```

and rewrite `readIntensity`:

```ts
export function readIntensity(): Intensity {
  const v = typeof localStorage === "undefined" ? null : localStorage.getItem(KEY);
  if (v && v in LEVELS) return v as Intensity;
  if (v && v in LEGACY) return LEGACY[v];
  return "steady";
}
```

- [ ] **Step 4: Fix the SettingsView default and copy**

In `frontend/components/SettingsView.tsx:18` change `useState<Intensity>("intense")` to `useState<Intensity>("steady")`, and at line 90 change the hint `default intense` to `default steady`. Update the `<p>` description to mention five levels. The `<select>` enumerates `Object.keys(LEVELS)` so it needs no change.

- [ ] **Step 5: Run all tests + typecheck**

Run: `cd frontend && npm test && npm run typecheck`
Expected: PASS. Update any test still asserting the old `intense`/`calm` names.

- [ ] **Step 6: Verify load-bearing, then commit**

Revert `LEGACY` lookup to plain fallback, confirm the migration test FAILS, restore, then:

```bash
git add frontend/lib/cosmos.ts frontend/components/SettingsView.tsx frontend/components/Cosmos.test.tsx
git commit -m "fix(ui): migrate legacy intensity names"
```

---

### Task 3: Verify the scene actually changes

Green unit tests do not prove pixels moved — the owner has caught this before.

- [ ] **Step 1:** Serve locally (`references/frontend-local-verify.md` — standalone: `node .next/standalone/server.js`, copy `.next/static`; plain `next start` 400s its own chunks).
- [ ] **Step 2:** In Playwright, set each tier and capture canvas pixel checksums across rAF frames; `still` must be static, `supernova` must differ most.
- [ ] **Step 3:** Confirm `prefers-reduced-motion` still forces `still` — existing test covers it, keep it.

Skipped: per-device auto-tuning. Add when someone reports jank on low-end hardware.
