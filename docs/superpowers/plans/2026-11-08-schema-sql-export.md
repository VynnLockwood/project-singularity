# Schema → SQL dump export — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export the schema the user designed as a `.sql` file (`CREATE TABLE` DDL) so Singularity can be used as a DB-design playground whose output is loaded into another product's Postgres.

**Architecture:** Generate DDL **in-app from the schema graph**, do NOT shell out to `pg_dump`. A real `pg_dump` would dump Singularity's own Postgres — carrying its internal tables, roles and schemas — which is the wrong artifact entirely. Add `GET /api/schema/export?format=sql` returning `text/sql`, built from the same graph that already backs `/api/schema/graph` (`backend/schema/views.py`, wired at `config/urls.py:31`). Frontend adds a Download button on the Schema tab using a blob URL — no dependency.

**Tech Stack:** Django 5, Python 3.11, Postgres DDL; Next.js 15 frontend. Tests: pytest + a real scratch Postgres round-trip.

**Spec:** `docs/superpowers/specs/2026-11-08-five-ui-gaps-design.md` §4

## Global Constraints

- Only user-designed tables are exported — not Singularity's internal relations.
- Every identifier must be quoted (`"name"`) and every literal escaped; this output is loaded into someone else's database.
- No shelling to `pg_dump`. No new dependencies — plain string building.
- The export endpoint must be anon-gated: 401 unauthenticated.
- In-container checks need `DJANGO_SETTINGS_MODULE=config.settings`.

---

### Task 1: DDL generator

**Files:**
- Create: `backend/schema/export.py`
- Test: `backend/tests/test_schema_export.py`

**Interfaces:**
- Consumes: the schema graph used by `schema/views.py:graph`
- Produces: `def to_sql(graph: dict) -> str` returning complete DDL text

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_schema_export.py`:

```python
from schema.export import to_sql

GRAPH = {
    "tables": [{
        "name": "district",
        "columns": [
            {"name": "district_id", "type": "integer", "nullable": False, "isPrimaryKey": True},
            {"name": "name", "type": "text", "nullable": True},
        ],
        "foreignKeys": [],
    }]
}

def test_emits_create_table_with_quoted_names():
    sql = to_sql(GRAPH)
    assert 'CREATE TABLE "district"' in sql
    assert '"district_id" integer NOT NULL' in sql
    assert "PRIMARY KEY" in sql
```

Read `backend/schema/views.py` `graph()` first to match its real key names and shapes — the values above are illustrative; if the real graph differs, use the real shape.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && pytest tests/test_schema_export.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `backend/schema/export.py`**

```python
"""Render the designed schema as Postgres DDL. Built here rather than via pg_dump,
which would instead dump Singularity's own database."""

def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'

def to_sql(graph: dict) -> str:
    out = []
    for t in graph.get("tables") or []:
        cols, pk = [], []
        for c in t.get("columns") or []:
            parts = [_ident(c["name"]), c["type"]]
            if not c.get("nullable", True):
                parts.append("NOT NULL")
            cols.append(" ".join(parts))
            if c.get("isPrimaryKey"):
                pk.append(_ident(c["name"]))
        if pk:
            cols.append(f"PRIMARY KEY ({', '.join(pk)})")
        fks = []
        for fk in t.get("foreignKeys") or []:
            fks.append(
                f"ALTER TABLE {_ident(t['name'])} ADD CONSTRAINT {_ident(fk.get('name') or f'fk_{t[chr(39)+chr(39)]}')}"
            )
        out.append("CREATE TABLE " + _ident(t["name"]) + " (\n  " + ",\n  ".join(cols) + "\n);")
        for fk in t.get("foreignKeys") or []:
            out.append(
                f"ALTER TABLE {_ident(t['name'])} ADD FOREIGN KEY ({_ident(fk['column'])}) "
                f"REFERENCES {_ident(fk['references']['table'])} ({_ident(fk['references']['column'])});"
            )
    return "\n".join(out) + "\n"
```

Adjust keys to whatever the real graph provides; keep `_ident` quoting in every path.

- [ ] **Step 4: Run the test**

Run: `cd backend && pytest tests/test_schema_export.py -v`
Expected: PASS.

- [ ] **Step 5: Add a FK case**

Extend the test with a table having a `foreignKeys` entry and assert an `ALTER TABLE ... REFERENCES` line is produced.

- [ ] **Step 6: Commit**

```bash
git add backend/schema/export.py backend/tests/test_schema_export.py
git commit -m "feat(schema): render designed schema as SQL DDL"
```

---

### Task 2: Export endpoint

**Files:**
- Modify: `backend/schema/views.py`
- Modify: `backend/config/urls.py:30-35`
- Test: `backend/tests/test_schema_export.py`

**Interfaces:**
- Consumes: `to_sql(graph)` from Task 1, existing `graph()` view
- Produces: `GET /api/schema/export?format=sql`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_schema_export.py`:

```python
@pytest.mark.django_db
def test_export_requires_login(client):
    r = client.get("/api/schema/export?format=sql")
    assert r.status_code == 401

@pytest.mark.django_db
def test_export_returns_sql_content_type(client, admin):
    r = client.get("/api/schema/export?format=sql")
    assert r.status_code == 200
    assert "text/sql" in r["Content-Type"]
    assert "Content-Disposition" in r and r["Content-Disposition"].endswith('.sql"')
```

Use the repo's existing auth fixtures — check `backend/tests/` for the established `admin`/`client` fixtures before writing.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && pytest tests/test_schema_export.py -v`
Expected: FAIL — no route.

- [ ] **Step 3: Implement**

In `backend/schema/views.py`, add a view following the existing view style (check `graph()` for its auth decorator / `login_required` pattern) that calls the same graph builder as `graph()`, passes it to `to_sql`, and returns `HttpResponse(sql, content_type="text/sql")` with:

```python
resp["Content-Disposition"] = 'attachment; filename="singularity-schema.sql"'
```

Register in `backend/config/urls.py` near line 31:

```python
    path("api/schema/export", schema_views.export),
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && pytest tests/test_schema_export.py -v`
Expected: PASS all.

- [ ] **Step 5: Commit**

```bash
git add backend/schema/views.py backend/config/urls.py backend/tests/test_schema_export.py
git commit -m "feat(schema): SQL export endpoint"
```

---

### Task 3: The round-trip test (this is the real proof)

Unit tests on SQL text prove nothing about whether the output loads. This task is mandatory.

**Files:**
- Test: `backend/tests/test_schema_export.py`

- [ ] **Step 1:** Using the scratch-Postgres setup documented in `references/backend-pytest-scratch-postgres.md`, load the exported SQL into a fresh database via `psql -f`.
- [ ] **Step 2:** Assert the expected tables, columns and FK constraints actually exist there (query `information_schema`).
- [ ] **Step 3:** Assert Singularity-internal tables are absent from the output — grep the SQL for any internal table name and assert zero hits.

Skipped: seed-data export, non-Postgres dialects. Add when requested.

---

### Task 4: Schema tab Download button

**Files:**
- Modify: `frontend/components/SchemaView.tsx` (find the Schema tab component)
- Modify: `frontend/lib/api.ts`

**Interfaces:**
- Consumes: `GET /api/schema/export?format=sql`
- Produces: a Download button

- [ ] **Step 1: Write the failing test**

In the Schema tab component's existing test file:

```tsx
  it("offers a SQL download", async () => {
    await setup();
    expect(screen.getByRole("button", { name: /download|export/i })).toBeInTheDocument();
  });
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run components/SchemaView.test.tsx -t "SQL download"`
Expected: FAIL — no button.

- [ ] **Step 3: Implement**

In `frontend/lib/api.ts` add:

```ts
export const SCHEMA_EXPORT_URL = "/api/schema/export?format=sql";
```

and in the Schema tab render:

```tsx
      <a className="link" href={SCHEMA_EXPORT_URL} download="singularity-schema.sql">
        Download SQL
      </a>
```

Prefer a plain `<a download>` over a fetch+blob — fewer moving parts, same result. No new dependency.

- [ ] **Step 4: Run tests + typecheck**

Run: `cd frontend && npm test && npm run typecheck`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/api.ts frontend/components/SchemaView.tsx
git commit -m "feat(ui): download the schema as SQL"
```

---

### Task 5: Prod verification

- [ ] **Step 1:** Deploy `web` + `api`.
- [ ] **Step 2:** `curl -s https://singularity.lunariankids.com/api/schema/export?format=sql -o /tmp/x.sql` anonymously → must be **401**.
- [ ] **Step 3:** With a session → 200, `text/sql`, and the file must contain `CREATE TABLE` for `district`/`province`/`subdistrict`.
- [ ] **Step 4:** Load `/tmp/x.sql` into a scratch Postgres and confirm those tables exist.
