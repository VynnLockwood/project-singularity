# Singularity: five UI/gaps fixes

Date: 2026-11-08
Status: draft, awaiting approval

## Context

Five reported gaps, each investigated against the running code and live prod
before writing. Findings shape every section below; the two most important:

- **Loading overlay is genuinely missing on pagination.** `GridEditor` never
  calls `setBusy`. The overlay added in `9176f4b` is wired only to chat/queue.
- **The labels toggle is not broken — it never runs for your session.** Prod
  renders FK columns as `<select>` edit controls for a user with edit rights.
  `displayText` only applies to read-only `<span>` cells, so toggling does
  nothing visible. This is the real root cause.

### Evidence

- `frontend/components/GridEditor.tsx:14` — `const PAGE = 50;`
- `frontend/lib/cosmos.ts:9-11` — three levels: reduced / calm / intense
  (intense = 3200 particles)
- `backend/config/siconfig.py:28` — `chat: {provider: "openrouter", defaultModel: "", allowedModels: []}`
- `backend/chat/openrouter.py:6` — `OPENROUTER_BASE_URL` env override exists
- No `pg_dump`/export anywhere in backend or frontend — nothing to reuse
- Live prod, Data tab, subdistrict: `district_id` cell renders a `<select>`
  of Thai district names, confirming item 5's cause

---

## 1. Chat model + provider settings (web)

**Scope: the Chat tab's LLM. Not** duplicate/schema search tuning.

The config already exists (`chat.provider`, `chat.defaultModel`,
`chat.allowedModels`) and the OpenRouter client already honours
`OPENROUTER_BASE_URL`. What is missing is the ability to change it from the UI.

Add a **Settings → Chat** section with:

- Provider select: `openrouter`, `opencode`
- Model input/free select, constrained by `allowedModels` when non-empty
- Persist to `singularity.yaml` through the existing settings write path

Two open points to resolve during implementation:

1. **`opencode` has no client yet.** Recommend adding `chat/opencode.py`
   mirroring `openrouter.py` (same `ChatError` contract), since both speak the
   OpenAI `/chat/completions` shape. Small, reuses the existing module shape.
2. **API key storage.** Must NOT go into the frontend or be echoed back.
   Keep server-side in `singularity.yaml`/env; the UI gets a boolean "key set"
   indicator, never the value.

**Verification:** set provider via UI → Chat request actually routes to that
base URL (assert on outbound host in a test); unset/missing key → clear error,
no key leakage in any response body.

---

## 2. Rows-per-page + loading on page change

Two independent fixes in `GridEditor`.

- Replace `const PAGE = 50` with a selectable size: 25 / 50 / 100 / 200,
  default 50. Thread through existing `limit` param — the API already accepts
  it, no backend change.
- Call `setBusy(true)` around every refetch, including Next/Prev, and clear in
  a `finally`. This routes through the overlay added in `9176f4b`.

The `setBusy` omission is the root cause of "no animation" — fix it once at the
fetch site, not per-button.

**Verification:** unit test asserting `onBusy(true)` fires on page change; the
existing overlay must be observed visible during a slow fetch in a real
browser.

---

## 3. Five animation tiers + rename

Extend `LEVELS` in `lib/cosmos.ts` from 3 to 5, ascending, with clearer names:

| new name | old | particles | stars | streak | glow |
|---|---|---|---|---|---|
| `still`    | reduced | 0    | 160 | 0   | 0 |
| `subtle`   | calm    | 700  | 160 | 1   | 1 |
| `steady`   | intense | 3200 | 320 | 2.2 | 2 |
| `vivid`    | new     | ~6000 | 480 | 3.0 | 3 |
| `supernova`| new     | ~10000 | 700 | 4.0 | 4 |

Migration: localStorage key is `singularity.cosmos_intensity`. Old values
`reduced`/`calm`/`intense` must map to `still`/`subtle`/`steady` —
`getIntensity()` currently validates against `LEVELS`, so a stale value would
silently fall back to default. Add explicit mapping.

The ceiling is real: this is GPU-bound on the client. Leave the particle counts
as tunable constants and cap `supernova` conservatively; measure on a low-end
device before raising.

**Verification:** existing `Cosmos.test.tsx` covers reduced/intense — extend to
assert each tier renders and that legacy values migrate.

---

## 4. SQL dump export (schema → .sql)

**New capability; nothing exists to reuse.** This is the "playground for DB
design" requirement: export a designed schema as a `.sql` file to use in
another product.

Recommend **generate DDL from the schema graph in-app**, not shelling to
`pg_dump`:

- A real `pg_dump` needs DB credentials and dumps Singularity's own Postgres,
  which is the wrong shape (it carries Singularity's internal tables and
  roles). The design intent is "give me CREATE TABLE for the tables I drew".
- Add `GET /api/schema/export?format=sql` returning `text/sql`, building
  `CREATE TABLE` from the schema graph (columns, types, PK, FK constraints).
- Frontend: a Download button on the Schema tab. No dependency — plain string
  building, and a `<a download>` for the blob.

Optional follow-up (not in this change): seed-data option, other dialects.

**Verification:** export a known schema, then load the produced `.sql` into a
scratch Postgres and assert the tables/columns/FKs exist. That round-trip is
the only check that actually proves it works — round-trip through a real DB,
don't just eyeball the SQL text.

---

## 5. Labels toggle must also apply to editable FK cells

**This is the actual bug.** Fix it where all FK rendering routes through.

Today:
- read-only FK cell → `<span>` → `displayText()` applies → shows label
- editable FK cell → `<select>` → `displayText()` never called → shows raw id

Fix: apply the same resolved label to `<select>` too. Two options:

- **(A) Recommended** — keep the `<select>` (editing must still show real
  options) but set the currently-selected option's **display text** to the
  resolved label when labels mode is on. Editing still works; you just read a
  name instead of an id. Also render read-only mode's label for consistency.
- **(B)** Force read-only display always when labels on. Smaller diff, but
  silently removes editing — rejected, it trades away a real capability.

Also fix the inverse case worth checking while in here: unresolved ids must
render `#id` and expose the true value on hover, so unresolved never looks
resolved.

**Verification:** extend `GridEditor.test.tsx` with an **editable-role** case
(the current test only covers `viewer`, which is exactly why this shipped
broken). Assert: labels on → label text shown; labels off → id shown. Then
confirm on live prod with your own session.

---

## Ordering

Suggested sequence, cheapest and most independent first:

1. §2 rows-per-page + loading (small, two known root causes)
2. §5 labels fix (small, one function)
3. §3 animation tiers (small, one map + migration)
4. §1 chat settings (medium, new provider module)
5. §4 SQL export (largest, new endpoint + round-trip test)

## Open questions

- §1: does `opencode` need its own client module, or does it share OpenRouter's
  exactly? Assumed yes-share-shape / thin module.
- §4: should export include only user-designed tables, or everything?
  Assumed: user-designed tables only.
- §3: exact particle counts for the two new tiers are estimates — tune by eye
  on a low-end device.
