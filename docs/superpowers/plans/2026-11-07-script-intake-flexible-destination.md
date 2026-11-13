# Script Intake: Flexible Destination, External Refs, Human Approval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `load_subdistricts.sql`-style scripts (`INSERT ... SELECT FROM (VALUES ...) JOIN parent`) pass intake with every row still inspected, give users a destination picker, and keep script loads behind human approval.

**Architecture:** The `(VALUES ...)` list becomes the row stream exactly as today; the JOIN becomes a `ref` with `"external": true` whose parent lives outside the script, resolved by the existing `_parent_keys()` at validate time. Row-level inspection (`_orphans`) is untouched — that is what keeps this safe rather than permissive. MCP may submit but never apply: `approve_change`/`reject_change` move to the approver role so editor tokens cannot see them.

**Tech Stack:** Django 5, pglast, psycopg 3, React/Next (frontend), existing `scripts` + `gate` + `approvals` + `mcp_server` Django apps. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-07-script-intake-flexible-destination-design.md` — read it; this plan argues from it.

## Global Constraints

- **Row inspection is inviolable.** No change may skip `_orphans` / `_parent_keys` row checking. This is the whole safety model.
- `"external"` defaults to **false**; every existing script keeps today's behaviour untouched.
- One-hop joins only. Multi-hop, CTEs, `ON CONFLICT`, `WITH`, `RETURNING`: still rejected.
- `bulk_insert` stays in `INTERNAL_OPS` (`gate/operations.py:42`); the normal `propose_change` path must still refuse it.
- Max identifier length 63 (`MAX_IDENT`, `scripts/mapping.py:58`).
- Tests: `pytest` from `/root/project-singularity/backend`. DB-backed tests need `TEST_PG_ADMIN_USER=postgres PGHOST=db`; pure-parser tests (`tests/test_script_sqlparse.py`) need no DB.
- Commit per task. Real-browser verification against `https://<your-host>` after deploy.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/scripts/sqlparse.py` | Parse the JOIN form; emit the VALUES list as the stream. Adds `_join_hint`. |
| `backend/scripts/mapping.py` | Allow external parents and existing targets. |
| `backend/approvals/service.py` | Require approver for script changes (closes self-approval hole). |
| `backend/mcp_server/tools.py` | Raise `approve_change`/`reject_change` to approver. |
| `frontend/components/ScriptsTab.tsx` | Destination picker writing the same mapping JSON. |
| `frontend/components/Cosmos.tsx`, `frontend/lib/cosmos.ts`, `frontend/components/SettingsView.tsx` | Animation intensity setting. |

---

### Task 1: Parse `INSERT ... SELECT FROM (VALUES ...) JOIN`

**Files:**
- Modify: `backend/scripts/sqlparse.py:720-746` (`insert`), `sqlparse.py:61-66` (`ParsedScript`)
- Test: `backend/tests/test_script_sqlparse.py`

**Interfaces:**
- Consumes: `ast.InsertStmt` (pglast)
- Produces: `ParsedScript.join_hints: dict[str, list[dict]]` — table → `[{"parent": str, "parent_key": str, "source_column": str, "line": int}]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_script_sqlparse.py

def test_insert_select_from_values_join_is_a_stream():
    sql = (
        "INSERT INTO master.subdistrict (code, name, district_code) "
        "SELECT v.code, v.name, v.district_code "
        "FROM (VALUES ('100101', 'Bangkok', '1001')) AS v(code, name, district_code) "
        "JOIN master.district d ON d.code = v.district_code;"
    )
    p = parse_script(sql)
    assert p.errors == [], p.errors
    assert list(p.streams["subdistrict"].columns) == ["code", "name", "district_code"]
    assert list(p.streams["subdistrict"].rows()) == [["100101", "Bangkok", "1001"]]
    assert p.join_hints["subdistrict"] == [
        {"parent": "master.district", "parent_key": "code",
         "source_column": "district_code", "line": 1}
    ]


def test_insert_select_two_joins_rejected():
    sql = (
        "INSERT INTO t (a, b, c) SELECT v.a, v.b, v.c "
        "FROM (VALUES ('1', '2', '3')) AS v(a, b, c) "
        "JOIN p1 ON p1.k = v.b JOIN p2 ON p2.k = v.c;"
    )
    p = parse_script(sql)
    assert len(p.errors) == 1 and "more than one JOIN" in p.errors[0].message


def test_plain_insert_values_still_works():
    p = parse_script("INSERT INTO t (a) VALUES ('1');")
    assert p.errors == []
    assert p.join_hints == {}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /root/project-singularity/backend && python -m pytest tests/test_script_sqlparse.py -k "join or plain_insert_values" -v`
Expected: FAIL — `only INSERT ... VALUES is supported`, and `join_hints` missing.

- [ ] **Step 3: Add the `join_hints` field**

```python
# sqlparse.py, ParsedScript (line 63)
@dataclass
class ParsedScript:
    ops: list[dict] = field(default_factory=list)
    streams: dict[str, RowStream] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)
    errors: list[ScriptIssue] = field(default_factory=list)
    statements: int = 0
    join_hints: dict[str, list[dict]] = field(default_factory=dict)  # table -> [ref hint]
```

- [ ] **Step 4: Relax the reject and capture the join**

Replace the guard at `sqlparse.py:725-728`:

```python
        sel = s.selectStmt
        if s.withClause or s.onConflictClause or s.returningClause:
            raise _Reject("INSERT with WITH / ON CONFLICT / RETURNING is not supported")
        if not isinstance(sel, ast.SelectStmt) or not sel.valuesLists:
            raise _Reject("only INSERT ... VALUES or INSERT ... SELECT FROM (VALUES ...) JOIN is supported")
        hint = self._join_hint(sel, line)
        if hint is None and any(getattr(sel, f) for f in (
                "targetList", "whereClause", "groupClause", "havingClause", "sortClause",
                "limitCount", "limitOffset", "withClause", "larg", "rarg", "distinctClause")):
            raise _Reject("only INSERT ... VALUES is supported")
```

Note `targetList` and `fromClause` are now expected on the SELECT form; `_join_hint` validates them instead.

- [ ] **Step 5: Add `_join_hint`**

```python
    def _join_hint(self, sel: ast.SelectStmt, line: int) -> dict | None:
        """One-hop `FROM (VALUES ...) AS v(cols) JOIN parent ON parent.key = v.col`."""
        frm = sel.fromClause or ()
        if not frm:
            return None
        joins = [j for j in frm if isinstance(j, ast.JoinExpr)]
        if len(joins) != 1 or joins[0].jointype != "JOIN_INNER":
            raise _Reject("a joined INSERT must have exactly one inner JOIN")
        j = joins[0]
        sub = j.larg
        if not isinstance(sub, ast.RangeSubselect) or sub.alias is None:
            raise _Reject("the joined INSERT must read FROM (VALUES ...) AS v(...)")
        if len(frm) != 1:
            raise _Reject("more than one JOIN in an INSERT ... SELECT is not supported")
        qual = j.quals
        if not isinstance(qual, ast.A_Expr) or qual.name != ("=",) or qual.kind != "AEXPR_OP":
            raise _Reject("the JOIN must be a single equality, parent.key = v.column")
        left, right = qual.lexpr, qual.rexpr
        parent_side, sub_side = left, right
        if _cols_of(sub_side)[0] != sub.alias.aliasname:
            parent_side, sub_side = right, left
        p_tbl, p_col = _cols_of(parent_side)
        s_tbl, s_col = _cols_of(sub_side)
        if s_tbl != sub.alias.aliasname:
            raise _Reject("the JOIN must relate the VALUES list to the parent table")
        if p_tbl is None or p_col is None or s_col is None:
            raise _Reject("the JOIN must be a single equality, parent.key = v.column")
        return {"parent": p_tbl, "parent_key": p_col, "source_column": s_col, "line": line}
```

Helper (module level, near `_const`):

```python
def _cols_of(node) -> tuple[str | None, str | None]:
    """(table, column) of a ColumnRef; either part may be None."""
    if not isinstance(node, ast.ColumnRef):
        return None, None
    parts = [f.sval for f in node.fields if hasattr(f, "sval")]
    if not parts:
        return None, None
    return (parts[-2] if len(parts) > 1 else None), parts[-1]
```

- [ ] **Step 6: Record the hint after `_add_stream`**

At `sqlparse.py:746`, after `self._add_stream(...)`:

```python
        if hint is not None:
            self.out.join_hints.setdefault(table, []).append(hint)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd /root/project-singularity/backend && python -m pytest tests/test_script_sqlparse.py -v`
Expected: all PASS (50 existing + 3 new).

- [ ] **Step 8: Commit**

```bash
git add backend/scripts/sqlparse.py backend/tests/test_script_sqlparse.py
git commit -m "feat(scripts): parse INSERT ... SELECT FROM (VALUES ...) JOIN into a stream plus join hint"
```

---

### Task 2: External refs in the mapping

**Files:**
- Modify: `backend/scripts/mapping.py:335` (parent must be in script), `mapping.py:262` (only own tables), `mapping.py:520-535` (emit refs)
- Test: `backend/tests/test_script_mapping.py`

**Interfaces:**
- Consumes: `ParsedScript.join_hints` (Task 1), mapping `refs[]` with `"external": true`
- Produces: gate ops with `bulk_insert` + `refs[{column, parent_table, parent_key, source_column}]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_script_mapping.py

EXT = {"tables": {"subdistrict_src": {
    "target": "subdistrict",
    "columns": {"code": "code", "name": "name", "postcode": "postcode"},
    "refs": [{"column": "district_id", "parent": "master.district", "parent_key": "code",
              "source_column": "district_code", "external": True}]}}}


def test_external_ref_builds_bulk_insert(external_state):
    parsed = parse_script(INSERT_SELECT_JOIN)
    res = apply_mapping(parsed, EXT)
    assert not res.issues, res.issues
    bulk = next(o for o in res.ops if o["op"] == "bulk_insert")
    assert bulk["table"] == "subdistrict"
    assert bulk["refs"] == [{"column": "district_id", "parent_table": "master.district",
                             "parent_key": "code", "source_column": "district_code"}]


def test_external_ref_false_still_requires_parent_in_script(external_state):
    m = copy.deepcopy(EXT)
    m["tables"]["subdistrict_src"]["refs"][0]["external"] = False
    res = apply_mapping(parse_script(INSERT_SELECT_JOIN), m)
    assert any("not a mapped table" in i.message for i in res.issues)
```

`external_state` is a fixture supplying `{"master.district": {"columns": {"district_id": {...}, "code": {...}}, "primary_key": ["district_id"], "unique": [["code"]]}}`. Add it in this task.

- [ ] **Step 2: Verify they fail**

Run: `cd /root/project-singularity/backend && python -m pytest tests/test_script_mapping.py -k external_ref -v`
Expected: FAIL — "parent 'master.district' is not a mapped table of the script".

- [ ] **Step 3: Allow external parents**

At `mapping.py:335`:

```python
            external = bool(r.get("external"))
            if external:
                if parent == n:
                    bad(rp, f"a table cannot refer to itself in a load ({n!r}); keep the column as a plain value")
                    continue
                if parent not in (external_state or {}):
                    bad(rp, f"external parent {parent!r} does not exist")
                    continue
                cols = (external_state[parent].get("columns") or {})
                if pkey not in cols:
                    bad(rp, f"parent_key {pkey!r} is not a column of external parent {parent!r}")
                elif pkey not in _unique_cols(external_state, parent):
                    bad(rp, f"external ref requires parent_key {pkey!r} to be unique in {parent!r}")
                ref_src[n].setdefault(sc, rc)
                continue
            if parent not in tables or parent not in srcs:
                bad(rp, f"parent {parent!r} is not a mapped table of the script")
                continue
```

`apply_mapping(parsed, mapping, external_state=None)` gains one optional argument; existing callers are unchanged.

- [ ] **Step 4: Allow loading into an existing target**

At `mapping.py:262`, only object when the target is neither created nor already existing:

```python
    existing = set((external_state or {}))
    for t in foreign:
        if t in existing:
            continue  # a mapped script may also load rows into a table that already exists
        bad(f"tables.{t}", f"the script changes table {t!r}, which it neither creates nor loads rows into; "
                           "a mapped script may only change tables it creates")
```

- [ ] **Step 5: Run tests**

Run: `cd /root/project-singularity/backend && python -m pytest tests/test_script_mapping.py tests/test_script_sqlparse.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/scripts/mapping.py backend/tests/test_script_mapping.py
git commit -m "feat(scripts): allow refs whose parent already exists (external refs)"
```

---

### Task 3: Wire external refs through validate + orphan checking

**Files:**
- Modify: `backend/scripts/validate.py` (`_parent_keys`, `_orphans`, `validate`)
- Test: `backend/tests/test_script_validate_external_ref.py` (new)

**Interfaces:**
- Consumes: gate ops from Task 2
- Produces: `report["tables"][*]["orphans"]`, `report["tables"][*]["sample_orphans"]`

- [ ] **Step 1: Write the failing test — this is the one that proves row inspection survived**

```python
def test_external_ref_flags_orphan_rows(live_state):
    # '9999' is not a district code in master.district
    parsed = parse_script(INSERT_SELECT_JOIN_WITH_BAD_CODE)
    rep = validate(parsed, EXT_MAPPING, live_state)
    t = next(t for t in rep["tables"] if t["name"] == "subdistrict")
    assert t["rows"] == 2
    assert t["orphans"] == 1, t            # the bad row is caught
    assert "9999" in t["sample_orphans"][0]
```

- [ ] **Step 2: Verify it fails** — Run the new test file; expected FAIL (external refs not yet plumbed through).

- [ ] **Step 3: Pass external state into `_parent_keys`**

`_parent_keys(parent, key, ops, bulks, parsed, state, norm)` already reads existing DB keys when
`key in state[parent]["columns"]` (`validate.py:142-147`). Only the *reachability* changes: `_orphans`
must consult `state` for external parents too. In `_orphans`, replace the parent-key lookup cache line:

```python
            cache[ck] = _parent_keys(*ck, ops, bulks, parsed, state, norm)
```

`state` comes from `schema.introspect.snapshot()` in `validate()` and already contains
`master.district` with its unique constraint on `code`, so the existing DB branch applies unchanged.
Add one assertion where the external parent is accepted, so a future change cannot silently skip it:

```python
    assert key in state.get(parent, {}).get("columns", {}), \
        f"external parent {parent} missing from state — orphan checks would silently pass"
```

- [ ] **Step 4: Run tests** — all PASS including the orphan count of 1.

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/validate.py backend/tests/test_script_validate_external_ref.py
git commit -m "feat(scripts): orphan-check external-ref loads against the live parent table"
```

---

### Task 4: Close the self-approval hole

**Files:**
- Modify: `backend/approvals/service.py:50-53`
- Modify: `backend/mcp_server/tools.py:29-30`
- Test: `backend/tests/test_approvals_self_approval.py`, `backend/tests/test_mcp_http.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_approvals_self_approval.py
def test_editor_cannot_approve_own_script_change(editor, script_change_by_editor):
    assert not can_decide(editor, script_change_by_editor)


def test_editor_can_still_approve_own_plain_change(editor, plain_change_by_editor):
    assert can_decide(editor, plain_change_by_editor)


def test_approver_can_approve_script_change(approver, script_change_by_editor):
    assert can_decide(approver, script_change_by_editor)
```

```python
# tests/test_mcp_http.py
def test_editor_token_does_not_see_approve_tools(client, editor_token):
    r = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    HTTP_AUTHORIZATION=f"Bearer {editor_token}")
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "approve_change" not in names and "reject_change" not in names
    assert "submit_script" in names
```

- [ ] **Step 2: Verify they fail** — expected: editor self-approval currently allowed; tools currently listed.

- [ ] **Step 3: Require approver for script changes**

```python
def can_decide(user, change: Change) -> bool:
    if change.source == "script" or change.kind in ("schema", "config"):
        # structure, rules, settings, roles and every script load: approvers only
        return user.can_approve
    return user.can_approve or (user.can_edit and user.pk == change.requester_id)
```

- [ ] **Step 4: Raise the MCP roles**

```python
    "approve_change": "approver",
    "reject_change": "approver",
```

- [ ] **Step 5: Run tests** — all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/approvals/service.py backend/mcp_server/tools.py backend/tests/
git commit -m "fix(approvals): script loads require an approver; hide approve tools from editors"
```

---

### Task 5: Real-file verification

No new code — proof the four tasks actually work together.

- [ ] **Step 1: Upload `load_subdistricts.sql` as script id 4 and validate with the external mapping**

```bash
docker compose cp /root/.hermes/cache/documents/doc_fd2e65abc6dd_load_subdistricts.sql api:/tmp/load_subdistricts.sql
```

- [ ] **Step 2: Time it and check orphans**

Expected: validate under 5s (it was 0.8s once the O(n²) scan was fixed — do not regress past 5s),
`report["tables"]` shows 7,720 rows for `subdistrict`, orphan count 0 against the 928 districts.

- [ ] **Step 3: Real browser** — `https://<your-host>`, Scripts tab, validate → HTTP 200,
`{"ok": true}` or explicit orphan list. No 504.

- [ ] **Step 4: Commit nothing** (no code changed); report numbers in the PR.

---

### Task 6: Destination picker UI

**Files:**
- Modify: `frontend/components/ScriptsTab.tsx` (raw JSON textarea at line 144)
- Test: `frontend/components/ScriptsTab.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
it("writes an external ref when resolving against an existing table", async () => {
  render(<ScriptsTab />);
  // pick existing table master.subdistrict, resolve district_id via master.district on code using district_code
  const json = await screen.findByTestId("mapping-json");
  expect(JSON.parse(json.textContent!)).toEqual({
    tables: { subdistrict_src: {
      target: "master.subdistrict",
      columns: { code: "code", name: "name", postcode: "postcode" },
      refs: [{ column: "district_id", parent: "master.district", parent_key: "code",
               source_column: "district_code", external: true }] } },
  });
});
```

- [ ] **Step 2: Verify it fails** — Run `npm test -- ScriptsTab`. Expected FAIL (no picker).

- [ ] **Step 3: Implement `DestinationPicker`**

New sub-component in the same file: create-new vs existing radio, target name input,
source-column → target-field `<select>` rows, one "resolve via" row per ref producing the
`"external": true` entry. Keep the existing textarea behind `<details><summary>Advanced: edit JSON</summary>`.

- [ ] **Step 4: Run tests** — PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/components/ScriptsTab.tsx frontend/components/ScriptsTab.test.tsx
git commit -m "feat(ui): destination picker for script intake writes the mapping JSON"
```

---

### Task 7: Cosmos animation intensity

**Files:**
- Modify: `frontend/components/Cosmos.tsx`, `frontend/lib/cosmos.ts`, `frontend/components/SettingsView.tsx`

- [ ] **Step 1: Write the failing test** — `frontend/components/Cosmos.test.tsx`: given `intensity="reduced"`, no rAF loop is scheduled.

- [ ] **Step 2: Verify it fails**

- [ ] **Step 3: Levels**

```ts
// lib/cosmos.ts
export const LEVELS = {
  reduced: { particles: 0,    stars: 0,   streak: 0,   glow: 0 },
  calm:    { particles: 700,  stars: 160, streak: 1,   glow: 1 },
  intense: { particles: 3200, stars: 320, streak: 2.2, glow: 2 },
} as const;
export type Intensity = keyof typeof LEVELS;
```

`loadCosmos(seed, level: Intensity)` picks counts from `LEVELS[level]`.

- [ ] **Step 4: Setting** — `cosmos_intensity` select in `SettingsView.tsx` Quality section, persisted per user via the existing settings API. `prefers-reduced-motion: reduce` forces `reduced` regardless (already read in `Cosmos.tsx:22`).

- [ ] **Step 5: Run tests** — PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/components/Cosmos.tsx frontend/lib/cosmos.ts frontend/components/SettingsView.tsx
git commit -m "feat(ui): cosmos intensity setting with reduced/calm/intense levels"
```

---

## Self-Review Notes

- Spec §1 → Tasks 1-3. §3 (roles) → Task 4. §2 (UI) → Task 6. §4 (cosmos) → Task 7. Task 5 is verification.
- Task 1 sets `join_hints`; Task 2 does not strictly consume it (the mapping JSON declares the ref
  explicitly). Keeping `join_hints` lets Task 6 prefill the picker from the parsed SQL — that is the
  only reason it exists. Do not add more consumers.
- No placeholders: every code block is complete. `_unique_cols` is referenced in Task 2 — add it as a
  small helper (`(external_state[t].get("unique") or [])` flattened) if no equivalent exists.

- [ ] **Step 4: Wire the setting** — `loadCosmos(seed, stars, particles)` already takes counts
  (`lib/cosmos.ts:41`); multiply by the level's factors in `Cosmos.tsx`. Persist `cosmos_intensity`
  (`reduced` | `calm` | `intense`, default `intense`) through the existing Settings API.
  `prefers-reduced-motion` forces `reduced` regardless (`Cosmos.tsx:20` already reads it).

- [ ] **Step 5: Run tests** — PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/components/Cosmos.tsx frontend/lib/cosmos.ts frontend/components/SettingsView.tsx
git commit -m "feat(ui): cosmos intensity setting (reduced / calm / intense)"
```

---

## Testing checklist (all tasks)

- [ ] `pytest backend/tests/` — full suite green
- [ ] Task 3's orphan-count test — row inspection demonstrably intact
- [ ] Task 4's MCP tool-list test — editors cannot see approve tools
- [ ] Task 5 — real file validates under 5s, correct orphan count
- [ ] Real-browser pass on prod after deploy (8 tabs, 0 console errors)
