# Script intake: flexible destination, external refs, agent-driven MCP

**Date:** 2026-11-07  **Status:** approved  **Repo:** VynnLockwood/project-singularity

## Why

`load_subdistricts.sql` (441 KB, 7,720 Thai subdistricts) is valid PostgreSQL but is rejected:

```
[parse] line 6:    only INSERT ... VALUES is supported
[parse] line 7733: SELECT is not allowed in a script
[parse] line 7734: SELECT is not allowed in a script
```

Line 6 is `INSERT ... SELECT v.* FROM (VALUES ...) JOIN master.district d ON d.code = v.district_code`.
The rows only exist *after* the join runs, so there is nothing to inspect before apply — and inspecting
every row is the whole safety model.

Chosen direction (**structured-flexible**): Singularity resolves the join *itself* at validate time, so
every row is still inspected. Not permissive, and not execute-then-audit.

The machinery already exists. `mapping.py` `refs` already resolves `source_column` against a parent's
`parent_key` and `_parent_keys()` already reads real keys out of the database. Three guards merely assume
the parent must live *inside* the script. This spec removes those three assumptions.

Current state: `master.district` has 928 rows (`district_id` PK, `code` unique), `master.subdistrict` is empty.

## Decisions

| Question | Decision |
|---|---|
| Keep safety while accepting valid SQL? | **Structured-flexible** — Singularity computes the rows itself, every row still inspected |
| External parent vs new wizard? | **Extend refs to external parents**, keep the JSON contract, put a picker UI on top |
| Trailing `SELECT count(*)`? | **Accept**, expose to MCP callers after apply |
| MCP intake ceiling? | **Full** — MCP can submit *and* approve |

## §1 External refs (approved)

### Data flow

```
INSERT ... SELECT v.* FROM (VALUES ...) JOIN district
        │
   sqlparse.insert()            accepts the JOIN form
        │                       stream = the VALUES list (every literal row is ours to read)
        ▼
   ParsedScript.streams["subdistrict_src"]
        │
   mapping.apply_mapping()      refs[] with "external": true
        │                       emits bulk_insert + refs
        ▼
   gate ops
        │
   validate._orphans()          _parent_keys() reads real district codes
        │                       every one of 7,720 rows checked
        ▼
   report: rows / orphans / sample_orphans
```

### Mapping shape

```json
{"tables": {"subdistrict_src": {
  "target": "subdistrict",
  "columns": {"code": "code", "name": "name", "postcode": "postcode"},
  "refs": [{"column": "district_id",
            "parent": "master.district",
            "parent_key": "code",
            "source_column": "district_code",
            "external": true}]}}}
```

`"external": true` defaults to **false**, so every existing script keeps today's behaviour. It relaxes
exactly one check and changes nothing else.

### Changes

| # | File | Change |
|---|---|---|
| 1 | `backend/scripts/sqlparse.py` `insert()` (line 728) | Accept `INSERT ... SELECT ... FROM (VALUES ...) JOIN parent ON parent.key = v.col`. Stream = the VALUES list. Record the join as a pending ref hint. Still reject WITH / ON CONFLICT / RETURNING, sub-fields, and joins deeper than one hop. |
| 2 | `backend/scripts/mapping.py` line 335 | Drop the `parent not in srcs` requirement when `external` is true |
| 3 | `backend/scripts/mapping.py` line 262 | Allow a target that already exists when the script only loads rows into it |

**Not changed:** `_parent_keys`, `_orphans`, gate ops, the executor. Row-level inspection is untouched —
that is the guarantee that makes this safe rather than permissive.

### Errors

| Message | Meaning |
|---|---|
| `more than one JOIN in an INSERT ... SELECT` | only one-hop joins are resolvable by us |
| `external parent master.district has no unique column code` | nothing to resolve against |
| `external ref ... is not unique in parent_key` | ambiguity, refuse rather than guess |

## §2 Destination picker UI

`ScriptsTab.tsx` today has a raw JSON `<textarea>` that is `JSON.parse`d. Replace it with a picker that
*writes the same JSON* — no new contract, no new backend shape.

```
Destination for source table "subdistrict_src"
  ( ) Create new table  → [ subdistrict        ]
  ( ) Existing table    → [ master.subdistrict ▼ ]

Columns                       ← loaded from describe_table / list_tables
  code        → code
  name        → name
  postcode    → postcode
  district_id ← resolve via  [ master.district ▼ ] on [ code ▼ ]  using [ district_code ▼ ]
  [x] drop province_name

[ Advanced: edit JSON ▾ ]     ← keeps today's textarea for escape hatches
```

- Source columns come from the validate report; target fields from `describe_table`.
- Create-new vs existing drives `target` plus whether a `create_table` op is emitted.
- The resolve row writes exactly the `refs` entry from §1 with `external: true`.
- Suggestion: `suggest_mapping()` already guesses a mapping — prefill from it, let the user correct.

Files: `frontend/components/ScriptsTab.tsx` (new `DestinationPicker` sub-component), no new endpoints.

## §3 Agent-driven MCP

Today MCP has 15 tools; `submit_script` and `validate_script` take no mapping, so an agent cannot drive
intake. Target: an agent can do the whole loop without the UI.

| Tool | Change |
|---|---|
| `validate_script` | accept `mapping` (optional). Returns `{ok, statements, errors, tables[], dry_run, gate}` |
| `submit_script` | accept `mapping` + `dry_run`. Returns the change id and the same report |
| `apply_script_mapping` | NEW — resolve an external ref and return orphan counts without storing anything |
| `approve_change` | already exists; stays role-gated (see risk) |

Role map (`mcp_server/tools.py` `TOOL_ROLES`) — **unchanged**, deliberately:

```
submit_script   editor
approve_change  editor
```

The owner's "MCP can submit AND approve" is satisfied by the existing editor role rather than by
lowering anything. An agent acts as the user whose token it carries, and tools above your role are
already hidden from your client.

### RISK — accepted, must be built with it

**MCP approve means an agent can be the approver.** That deliberately removes the human-in-the-loop on
`bulk_insert` for anyone holding an editor token. Required mitigations, not optional:

1. `bulk_insert` stays in `INTERNAL_OPS` — the normal `propose_change` path still refuses it. Only script
   intake can produce one. This is existing behaviour and must not be relaxed.
2. `approve_change` keeps its RANK gate; we do not add a new lower role.
3. Every MCP approval is recorded in the audit trail with the acting user, so "who approved this load" is
   always answerable.
4. Tokens are per-user and looked up on every request, so revoking one takes effect immediately.

Owner directive is security-first; this section is the one place the design trades isolation for
automation, so it ships with the mitigations above or not at all.

### Trailing SELECTs

Accepted, per decision. They are recorded on the script as `post_checks` and run by the caller after
apply — Singularity does not execute them. `get_script` returns them. They never become gate ops and
never touch the applied data.

## §4 Cosmos animation intensity

`frontend/components/Cosmos.tsx` is a canvas scene; `lib/cosmos.ts` builds the scene (Rust/WASM when
present, else the TS twin) with `loadCosmos(seed, stars=220, particles=1600)`.

One tunable, three levels — heaviest is the new default:

| Level | particles | stars | streaks | glow | notes |
|---|---|---|---|---|---|
| `reduced` | 0 | static | off | off | honours `prefers-reduced-motion` |
| `calm` | 700 | 160 | short | 1× | current look |
| `intense` | 3200 | 320 | long | 2× | new default, heavier/powerful |

- Setting: `cosmos_intensity` in Quality settings (SettingsView), persisted per user, `reduced` | `calm` | `intense`.
- `prefers-reduced-motion: reduce` (already read in `Cosmos.tsx`) forces `reduced` regardless of the setting.
- Laptop concern from the owner: because particle count scales cost linearly, a weak machine picks `calm`
  or `reduced` in Settings and it sticks. No auto-detection — user choice, as asked.
- Files: `components/Cosmos.tsx`, `lib/cosmos.ts` (accept a level), `components/SettingsView.tsx`, plus the
  existing settings persistence. ~30 lines total.

## Out of scope

- Multi-hop joins, CTEs, ON CONFLICT — still rejected, revisit only when a real file needs them.
- Executing trailing SELECTs server-side.
- Auto-detecting device capability for the animation.

## Testing

- `test_script_sqlparse.py`: external-ref mapping produces a `bulk_insert` with the right `refs`; every
  row still reaches `_orphans`.
- One test with a deliberately bad `district_code` asserting a non-zero orphan count — this is what proves
  row inspection survived the change.
- The real 441 KB file: validate under 5s, correct orphan count.
- `test_mcp_http.py`: submit with mapping over MCP returns `{ok: true}`.
- Real-browser check of the destination picker on prod after deploy.
