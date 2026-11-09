# Database tab, full export, site-managed chat model, audit-column hiding

Date: 2026-11-09. Status: design approved by the product owner in chat; this file is for written review.
Base: `origin/main` at c87aee0 (PRs 1 to 16 merged: script intake, SQL export, chat provider tunable).

## 0. Summary

Four changes, each its own implementation plan, in this order:

1. **Plan A: quick fixes.** Chat provider dropdown plus a model input managed on the website (no deploy); Duplicates tab stops showing audit columns.
2. **Plan B: full export.** Structure and optional data as one `.sql` file a Postgres admin can run.
3. **Plan C: multi-schema backend.** A "database" is a Postgres schema. New schema ops. Table ops take an optional `schema`.
4. **Plan D: Database tab.** Replaces the Schema tab: create schemas, manage tables and fields, ERD, export.

### Decisions taken (from the owner)

| Question | Decision |
|---|---|
| What is a "new database"? | A new Postgres **schema** in the same database. Not `CREATE DATABASE`, not an external server. |
| Model and provider on the site | Provider dropdown with exactly `openrouter` and `opencode`; a model input to add and select models; takes effect for new chats at once. API keys stay in the server environment (unchanged). |
| Export | Structure plus data with a toggle, one `.sql` file. |
| Audit columns | Hidden in the Duplicates tab and, by default, in the Database tab with a "Show audit columns" toggle. |

### Assumptions (change me if wrong)

- "The old schema tab" in the model request means the Schema tab's assistant, which opens the Chat tab, so it follows the chat model automatically. The embedding model used for schema search stays deploy-time (`embedding.model` in `singularity.yaml`); changing it would invalidate stored vectors.
- Audit columns are the roles from the `auditColumns` rule (`created_at`, `updated_at`, `created_by`, `is_active`, and the primary key `<table>_id`), found through `gate/conventions.py` (`every_name()`), never by hard-coded names.
- Existing tables stay in schema `master`. Nothing is moved.
- Only approvers change settings; only approvers approve schema changes (unchanged rules).

## 1. Plan A: quick fixes

### 1.1 Chat provider and model from the website

Today: `chat.provider` is already a tunable (choice). `chat.defaultModel` is a tunable constrained to `chat.allowedModels`, which is read from `singularity.yaml` only (`settings_app/tunables.py` OPTIONS, `chat/views.py` allowed set). Adding a model needs a file edit and a deploy.

Change:

- New setting `chat.models`: list of model ids stored in the database (Setting row, JSON list). The yaml `chat.allowedModels` becomes the seed: used only while `chat.models` has never been set.
- Settings tab, "Chat" card:
  - Provider: dropdown, options `openrouter`, `opencode`, with the existing "key set / missing" badge per provider.
  - Model: text input plus an **Add** button, and a list of saved models, each selectable as the default and removable. One model is the default (`chat.defaultModel`).
  - Selecting a provider or default model is an approver action, applied immediately like other tunables (audited).
- Validation (server side, in `tunables`): model id must match `^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,119}$`; at most 50 saved models; default must be one of the saved models; provider must be one of the two.
- `chat/views.py` allowed set = saved models (plus the default). New chats use the default; the chat header can still switch among saved models.
- The chat orchestrator must call the **selected provider's** client (`openrouter` or `opencode`); today `orchestrator.py` calls `openrouter.complete` directly. Add one dispatch function `chat/providers.py: complete(provider, messages, tools, model)` and use it.
- A provider with a missing key: selecting it is allowed but chat shows the existing `not_configured` error; the badge makes it visible.
- API keys are not stored or shown on the site (out of scope; keeps secrets out of the database).

### 1.2 Hide audit columns in the Duplicates tab

Backend matching already skips audit roles (`duplicates/service.py`). The record detail and compare view still list them. Change: the duplicates detail endpoint omits columns in `roles.hidden_columns()` plus the role columns (pk, created_*, updated_*, is_active) from the compared records, and the frontend `DuplicatesView` renders only what the endpoint returns. No toggle in this tab. Merge behavior is unchanged (merge keeps updating the same rows by primary key).

### Plan A tests

- tunables: add, remove, select model; reject bad id, 51st model, default not in list, unknown provider; non-approver cannot change.
- chat: new chat uses saved default; provider dispatch calls the right client (fake clients); missing key gives `not_configured`.
- duplicates detail JSON has no audit columns for a table that has them.
- frontend tests for the Settings card and DuplicatesView.

## 2. Plan B: full export (structure plus data)

Today `schema/export.py: to_sql(graph)` writes CREATE TABLE, PK and FK only.

### 2.1 Endpoint

`GET /api/schema/export?schema=<name>&data=0|1` (default `schema=master`, `data=0`).

- Structure: any signed-in role (viewer and up). Data: editor and up. A viewer asking `data=1` gets 403.
- Response: `text/sql` attachment `singularity-<schema>-<yyyymmdd>.sql`, streamed.

### 2.2 File content (in this order)

1. Header comment (generator, date, schema, whether data is included, row counts).
2. `CREATE SCHEMA IF NOT EXISTS "<schema>";` and `SET search_path`.
3. Per table: `CREATE TABLE` with columns, types, `NOT NULL`, defaults, identity (`GENERATED BY DEFAULT AS IDENTITY`), `PRIMARY KEY`, inline `UNIQUE` and named `CHECK` constraints.
4. `COMMENT ON TABLE/COLUMN`.
5. (data=1) per table in dependency order: `COPY "<schema>"."<table>" (cols) FROM stdin;` rows as COPY text format (`\N`, `\t`, `\\` escapes), terminated by `\.`.
6. After data: `ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY ... ON DELETE ...` (with data loaded first, so any load order works).
7. Indexes (`CREATE INDEX`/`CREATE UNIQUE INDEX`).
8. (data=1) identity fix: `SELECT setval(pg_get_serial_sequence(...), max, true)` per identity table.

### 2.3 Rules

- Every identifier double-quoted with embedded quotes doubled (existing `_ident`); literals through a single escape helper.
- Data is read through the read-only role in one `REPEATABLE READ` transaction (consistent snapshot), streamed with a server-side cursor, never loaded whole into memory.
- Caps: `limits.maxExportRows` (default 1,000,000 rows total; tunable). Over the cap: 413 with a message naming the table that crossed it. Export event is audited (`schema.export`, with schema, data flag, row counts).
- Tables with no columns are skipped (kept from today's behavior).
- Round trip is a test requirement: exported file loads into a scratch database with `psql`, and the result matches the source (column types, constraints, row counts, a checksum of each table's data).
- A "sample" mode is out of scope.

### Plan B tests

DDL for each feature (unique, check, default, identity, comment, FK on_delete); COPY escaping (tab, newline, backslash, `\N`, Thai text); dependency order with a cycle-free chain; viewer cannot get data; row cap; psql round trip including identity next value; audit event.

## 3. Plan C: multi-schema backend

### 3.1 Model

- A "database" in the UI is a Postgres schema owned by the designed-data roles. `master` remains and is the default. The internal `singularity` schema and `public` are never listed or touchable.
- Schema name rules: snake_case, `^[a-z][a-z0-9_]{0,62}$`, not `master`-reserved names (`singularity`, `public`, `pg_*`, `information_schema`).
- Limit: `limits.maxSchemas` (default 20, tunable).

### 3.2 New operations (gate)

- `create_schema {name, comment?}`, `rename_schema {schema, new_name}`, `drop_schema {schema, reason}`.
- `drop_schema` is refused if the schema is `master`, or has tables, unless a `reason` is given and the schema is empty. (Strict: drop only empty schemas; dropping tables first goes through `drop_table` with its own reason.)
- All three are schema-change operations: editors propose, approvers approve (existing rule). `drop_schema` and `rename_schema` are included in the `forbidden`/naming rule machinery like other ops.

### 3.3 Existing operations take `schema`

- Every table-targeting op gets an optional `schema` (default `master`). Cross-schema FKs are allowed only when both schemas are managed ones.
- Gate state (`gate/state.py`) is keyed by `schema.table` internally; with `schema=master` the public names stay as they are today so existing proposals, history and tests keep working. `fk_name`, `index_name`, `unique_name` stay table-based within a schema.
- `gate/sqlgen.py`, `schema/executor.py`, `schema/introspect.py`, `schema/rows.py`, `duplicates/service.py`, `search/indexer.py`, `scripts/validate.py` stop assuming the literal `master` and take the schema from the op or request. Introspection endpoints accept `?schema=`; default `master`.
- Script intake: the destination schema is `master` unless the user picks another managed schema in the destination picker.

### 3.4 Database roles and the one-time migration

- Roles `singularity_master_rw` / `singularity_master_ro` cannot create schemas today (no CREATE on the database). Plan: a one-time migration run **as the database owner** (not by the app) that:
  - creates a `SECURITY DEFINER` function `singularity.create_managed_schema(name text)` owned by the owner role, which creates the schema, sets owner to `singularity_master_rw`, grants USAGE to ro, sets default privileges for tables and sequences, and rejects names breaking the rules in 3.1;
  - creates `singularity.drop_managed_schema(name text)` (empty schemas only) and `rename_managed_schema(old, new)`;
  - grants EXECUTE only to the rw role.
- The executor calls these functions for the three new ops, inside the same transaction as the rest of the change (reverse ops captured: `create_schema` reverses to `drop_schema`, `rename_schema` to the opposite rename, `drop_schema` is irreversible).
- `db/init/01-schemas-roles.sh` gets the same statements for fresh installs. **The migration against the live database is never run by this work without the owner's explicit confirmation.** The plan includes a copy-paste SQL file for the owner and a check query.

### Plan C tests

Scratch-database fixtures with the real restricted roles (existing `pgsetup`): create, rename, drop empty, drop non-empty refused, reserved names refused, limit, rw cannot create schemas directly, ro cannot write, ops in a non-master schema (create_table, add_column, FK across schemas), executor reverse, `master` behavior unchanged (the existing suite must not gain failures), introspect per schema, search index keyed by schema.

## 4. Plan D: Database tab

### 4.1 Layout

Tab id `database` replaces `schema` in `AppShell` (label "Database"). Three areas:

- **Left rail:** schema list (with table counts), "New database" button, then tables of the selected schema, with search box.
- **Main:** selected table with sub-tabs: **Fields**, **Relations**, **ERD** (the existing diagram), **Overview** (the old Schema tab content: search box and assistant).
- **Header actions:** Export (structure / structure plus data, uses Plan B), Rename, Delete (when empty).

### 4.2 Managing things

All writes build the same structured operations the chat uses and go through the gate and approval (editors propose; approvers approve; viewers read only). The UI shows the existing change card and diff before sending.

- New database: name plus optional comment, validated live against the naming rules.
- Tables: create (name, columns with type, nullable, unique, default, references), rename, drop (reason required).
- Fields: add, rename, change type, set comment, drop (reason required), add index/unique/FK.
- Audit columns (the `auditColumns` roles) are hidden in Fields by default, with a **Show audit columns** toggle (state kept per browser). They stay read-only in the editor (as the grid does today).
- Empty states, loading and error states follow the existing app components; phone layout works (the app is already responsive).

### 4.3 Backend support

- `GET /api/schema/schemas` → `[{name, tables, comment}]` (managed schemas only).
- Existing `tables`, `graph`, `table_detail`, `query` endpoints take `?schema=`.
- No new write endpoints: writes use the existing change proposal endpoints with the new ops.

### Plan D tests

Component tests (Vitest, existing style): rail lists schemas and tables; new database form validation; fields table hides audit columns until toggled; proposing an add-field sends the right operation; viewer sees no write buttons; export menu calls the right URL; Overview tab still shows search and assistant. One browser-level smoke test is optional.

## 5. Settings and limits added

| Key | Default | Range | Plan |
|---|---|---|---|
| `chat.models` | seeded from yaml | up to 50 ids | A |
| `limits.maxExportRows` | 1,000,000 | 1,000 to 20,000,000 | B |
| `limits.maxSchemas` | 20 | 1 to 200 | C |

## 6. Milestones

- M1 Plan A (small, ships alone).
- M2 Plan B (ships alone).
- M3 Plan C (backend, behind no UI; safe to merge since `master` is the default everywhere).
- M4 Plan D (UI on top of M2 and M3).

## 7. Out of scope

- `CREATE DATABASE`, external servers, moving tables between schemas, copying a schema.
- Storing provider API keys on the site.
- Changing the embedding model from the site.
- pg_dump based export, partial or sampled data export, scheduled exports.
- Per-user model choice (the default is global; chats can switch among saved models as today).

## 8. Risks

- **Plan C touches many modules.** Mitigation: `schema` defaults to `master` everywhere, keep the existing suite green at each step, and add the schema parameter module by module with tests.
- **Role grants need an owner-run migration** on the live database. Mitigation: ship the SQL and check query; do not run it unasked.
- **Large exports** can hold a long read transaction. Mitigation: snapshot read, row cap, statement timeout, streaming.
- **Provider dispatch** changes the chat call path. Mitigation: fake-client tests for both providers and keep the `not_configured` error path.
- **Naming drift:** new constraint and index names stay snake_case and follow `gate/state.py` helpers.
