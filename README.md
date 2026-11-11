# Singularity

An internal tool for designing and managing a **master data** database in PostgreSQL through prompts. AI clients (a built-in web chat via OpenRouter, plus external MCP clients such as Claude Desktop or Cursor) change the database only through an MCP server, and every change passes a configurable rules gate and an approval flow.

- Design: [`docs/superpowers/specs/2026-11-05-singularity-design.md`](docs/superpowers/specs/2026-11-05-singularity-design.md)
- Milestone 1 plan: [`docs/superpowers/plans/2026-11-05-milestone-1-foundation.md`](docs/superpowers/plans/2026-11-05-milestone-1-foundation.md)

## Status

Milestones 1 to 7 are done: compose stack, nginx gateway, Postgres schemas and roles, Google sign-in, roles, audit log, the rules gate, structured operations and SQL generation, the change lifecycle with approvals, backups and revert, REST API, and the MCP server. The `worker` container is a placeholder until later milestones. Milestone 7 (3D universe) turns the Schema tab into a 3D view with a 2D toggle. Milestone 6 (Rust engine) adds the Duplicates tab and column stats. Milestone 5 (grid editor) adds the Data tab. Milestone 4 (search) adds the embed service and schema search. Milestone 3 (web chat) adds the signed-in app: a chat with the assistant (OpenRouter), a review tray with Approve / Reject that updates live over WebSocket, a review queue, and a 2D schema diagram.

## Web chat

Set `OPENROUTER_API_KEY` in `.env` (only the `api` container receives it) and manage the chat provider and models on the Settings page (approvers). `defaultModel` and `allowedModels` under `chat` in `singularity.yaml` only seed the first value; `maxToolRounds` and `messagesPerMinute` are set there too. The assistant has the same tools as an MCP client for your role, but no approve tool: the review tray is the only way to approve from chat, so the model cannot approve its own proposals. Changes proposed from any source (chat, MCP client) show up live for the requester and for approvers.

Assistant replies are shown as formatted text: lists, tables, bold and code blocks, with a Copy button on code. Raw HTML in a reply is never rendered and only web and mail links are clickable.

## Activity

The **Activity** tab has two sections. **Queue** lists the changes waiting for your decision. **History** lists every change ever made, newest first, and can be filtered by status, kind, database and "only mine" (the filters are in the address, so a link reproduces the view). Open a row to see the change; approvers can **Revert** an applied change that the server can undo automatically (it asks first, and the button disappears once a revert exists).

## Connect an AI client (MCP)

**Claude (recommended): OAuth.** Add `https://<your-host>/mcp` as a custom connector. Claude finds the sign-in service (`/.well-known/oauth-*`), registers itself (`/oauth/register`), sends you to a consent page after your normal Google sign-in, and receives a revocable access token (authorization code + PKCE S256) valid for 30 days plus a refresh token, so Claude renews it by itself. The connection then shows in the Connect tab, where you can revoke it. After updating Singularity, rebuild and restart the `mcp`, `api` and `web` containers: a client only sees the tools the running server offers (reconnecting just re-reads them), and `/healthz` on the mcp container reports how many it has.

**Manual links and tokens:** sign in, open the **Connect** tab, name the connection and press **Create connection link**. You get, once, ready-to-paste settings for each client (only a hash of the token is stored):

- **Claude** (claude.ai, Desktop, mobile): Settings, Connectors, Add custom connector, paste the link `https://<your-host>/mcp/t/sgl_...`.
- **Gemini CLI**: the `mcpServers` JSON for `~/.gemini/settings.json` (URL plus an `Authorization: Bearer` header).
- **Claude Code**: the `claude mcp add --transport http ...` command.

The link form puts the token in the address because those connectors cannot send a header; it can appear in server logs, so revoke links you stop using (also in the Connect tab). Clients that can send headers should use `https://<your-host>/mcp` with `Authorization: Bearer sgl_...`. A client gets the same tools as your role, and every change still goes through the rules gate and approvals.

### What an AI client can do

Every tool states the role it needs; a client only sees the tools its user's role allows. The web chat assistant has the same tools, except the two that decide a change.

| Tool | Needs | What it does |
|---|---|---|
| `get_rules` | viewer | The team's design rules. |
| `list_schemas`, `list_tables`, `describe_table` | viewer | Databases, tables, columns. |
| `search_schema` | viewer | Find where a kind of data belongs (Thai or English). |
| `read_rows` | viewer | One table, a page at a time, with sort and filters. No SQL needed. |
| `query` | viewer | One read-only `SELECT`. |
| `export_schema` | viewer (data: editor) | A database as SQL, inline up to `limits.maxInlineExportBytes`. |
| `list_changes`, `get_change` | viewer, editor | Change history and one change's diff. |
| `list_duplicates`, `get_duplicate` | viewer | Duplicate groups and their records side by side. |
| `get_settings` | viewer | Quality settings and every gate rule. |
| `propose_change` | editor | Structure and data changes as gated operations. |
| `propose_config_change` | editor | Settings, rules and roles. Always waits for an approver. |
| `recheck_change` | editor | Run the gate again on a stale change. |
| `find_duplicates`, `merge_duplicates`, `dismiss_duplicate` | editor | Scan a table, merge a group (a data change), or mark it as not duplicates. |
| `submit_script`, `validate_script`, `get_script` | editor | Load a pg_dump-style SQL script as gated operations. |
| `begin_load`, `append_rows`, `finish_load`, `get_load`, `cancel_load` | editor | Load thousands of rows (up to `limits.maxScriptRows`, default 200,000) into an existing table from chat, no shell needed. |
| `approve_change`, `reject_change`, `list_pending` | approver | Decide a change, after the user confirms. |
| `revert_change` | approver | Undo an applied change that has a stored reverse. |
| `get_audit_log` | approver | Who did what and when. |
| `list_users` | approver | People and their roles. |
| `reindex_search` | approver | Refresh the schema search index. |

## Raw SQL

**Loading big data over MCP.** `insert_rows` is for a few hundred rows. For more, an AI client calls `begin_load(table, columns)`, then `append_rows(load_id, seq, csv)` once per chunk of about 2,000 rows (header-less CSV; an unquoted empty field is NULL, `""` is the empty string; a failed call is retried with the same `seq`), then `finish_load(load_id)`. Nothing touches the database until `finish_load`, which runs the same checks as a script upload (rules gate, foreign keys, a rolled-back dry run) and submits one approvable change; undoing it deactivates the loaded rows. Optional `refs` fill a foreign key from a parent business key (districts by province code). Open loads expire 24 hours after their last chunk; limits are tunable in Settings (`limits.maxLoadChunkBytes`, `limits.maxLoadChunkRows`, `limits.maxLoadBytes`, `loads.ttlHours`, `loads.maxOpenPerUser`).

When no operation fits (functions, triggers, grants, bulk updates), an editor or approver can call `propose_sql` (MCP or the chat assistant) with the SQL and a reason of at least 10 characters.

- The SQL is parsed, checked against a refusal list (transaction control, `SET ROLE`, `COPY`, schema and role DDL, `CONCURRENTLY`, `VACUUM`, extensions) and dry-run in a rolled-back transaction. The rules gate is **not** applied.
- Only an approver can approve it, and only in the web app. No AI client can, even for an approver.
- On approval the database is backed up, then all statements run in one transaction. A failure changes nothing. It cannot be undone automatically.
- The limits `limits.maxSqlBytes` and `limits.maxSqlStatements` and the switch `sql.enabled` are in Settings. Switching it off hides the tool and refuses raw SQL changes.
- The database role is the real fence: it has no rights on the `singularity` schema and is not a superuser.

## Operations the gate understands

Every change is a list of these structured operations, never SQL. Each one is checked against the rules, shown
as a diff, and applied only after approval. "Undo" says whether Revert can reverse it.

| Operation | Kind | Undo |
|---|---|---|
| create_table, add_column, add_index, add_unique, add_foreign_key, add_check | schema | yes |
| rename_table, rename_column, alter_column_type, set_column_comment, set_table_comment | schema | yes |
| set_column_default, drop_column_default, set_not_null, drop_not_null | schema | yes |
| create_view | schema | yes |
| create_enum | schema | yes |
| add_enum_value, drop_enum, drop_view | schema | no |
| drop_column, drop_table, drop_index, drop_constraint | schema, needs a reason | no |
| insert_rows, update_rows, deactivate_rows | data | yes |
| upsert_rows | data | yes (up to the row limit) |
| delete_rows | data, needs a reason | yes (up to the row limit) |
| create_schema, rename_schema, move_table | database, goes alone | yes |
| drop_schema | database, needs a reason | no |

Enum type names can be used as column types once created. `get_rules` returns one example of each newer
operation, and `list_tables` also lists a database's views and enum types.

## Data grid

The Data tab edits one table at a time: server-side paging, sort and filter, reference columns as dropdowns (labelled by the target's name column), and a draft that collects your edits. **Save for approval** sends one proposal through the same rules gate as everything else: identical edits are grouped into one `update_rows`, new rows into one `insert_rows`, and removed rows are *deactivated* (`isActive = false`), never deleted. Editors approve their own data changes in the review tray. Viewers see the grid read-only.

Pick a database and table in the left pane; the address `/data/<database>/<table>` opens it directly, and the view lives in the query string (`?sort=name&dir=desc&inactive=1&f.name=abc`), so a link reproduces it. Filters are chips above the grid (case-insensitive contains, combined with AND); click a header to sort ascending, again for descending, a third time to clear. **Export CSV** (editors and approvers) downloads the whole filtered, sorted view as UTF-8 with a byte order mark so Excel reads Thai text, with raw ids for reference columns. A view with more rows than the setting `limits.maxExportRows` is refused with a message asking you to narrow the filters; nothing is cut off silently. Text starting with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading apostrophe so a spreadsheet cannot run it as a formula; numeric and boolean columns are not changed. Each export is written to the audit log as `table.export_csv`.

Limits: the grid does not detect that someone else changed the same cell after you loaded the page (last approved write wins), and one save is capped by `maxOpsPerProposal` / `maxRowsPerDataOp` in `singularity.yaml`.

## Search

Ask "where does this data belong?" in the Schema tab (Thai or English), or let the assistant call `search_schema`. Every table and column becomes a document (name, type, comment, relationships, a few sample values) embedded by the `embed` container (multilingual-e5-small, CPU, no internet at runtime: the model is baked into the image at build). Ranking blends meaning (pgvector, cosine) with name similarity (`pg_trgm`), and an exact name always ranks first. The index refreshes in the background after an applied schema change; approvers can rebuild it. If nothing scores above `search.minConfidence` (default 0.8), the UI offers to ask the assistant to propose a new column. That threshold was set without running the real model, so tune it against your own schema.

Known gaps: OAuth access tokens do not expire (revoke them in the Connect tab) and have no refresh flow, and there is no scheduled nightly backup yet (a backup is taken before every schema change).

## Duplicates and column stats

The `engine/` crate (Rust) does the heavy lifting twice from one codebase: as a Python module (PyO3) inside the backend for duplicate detection, and as WebAssembly in the browser for column statistics. Names are normalized (Unicode NFKC, Thai digits and zero-width marks, company suffixes like Co., Ltd., จำกัด), grouped by blocking keys, then scored with Jaro-Winkler, token-sorted Jaro-Winkler and bigram overlap; a group's score is its weakest link.

The **Duplicates** tab scans one table at a time (`duplicates.*` in `singularity.yaml`), lists groups with their score, and shows the records side by side. Pick the record to keep and **Merge**: this is an ordinary data change that moves every foreign-key reference to the survivor and then deactivates the others, so it goes through the same rules gate and approval. "Not duplicates" dismisses a group and a scan will not bring it back.

The **Column stats** button in the Data tab summarises the rows on screen (distinct, empty %, min/max/mean, top values). The browser build is optional: if `frontend/public/engine/` is missing, a TypeScript implementation that passes the same golden test cases (`engine/testdata/stats_cases.json`) is used. CI builds the WASM package and uploads it as an artifact.

Not verified in the build sandbox: the Docker image (Rust stage), the WASM build, and real browser behaviour.

## Settings

The **Settings** tab has three sections. **Quality**: search confidence, duplicate similarity, scan caps, query and proposal limits, chat model and rate limits, backup retention. Approvers change them and they apply within seconds (stored in the database over the values in `singularity.yaml`; Reset returns to the file's value; every change is audited). **Rules**: turn each gate rule on or off, switch it between block and warn, edit its parameters, see its history and restore an older version. **Team** (approvers): set people's roles. Deploy-time things (sign-in domains, the list of allowed models, the embedding model) stay in `singularity.yaml`.

**Changing settings from an AI client.** Settings, rules and roles can also be changed over MCP and in the web chat, but never directly: `get_settings` (viewer) reads them, `list_users` (approver) lists people, and `propose_config_change` (editor) proposes operations (`set_setting`, `reset_setting`, `update_rule`, `create_rule`, `set_user_role`; up to 20, not mixed with schema or data operations). The proposal is dry-run in a rolled-back transaction, then waits in the approval queue as a **Settings change**. Approvers (not the proposer) see it live in the review tray and Activity, the Settings tab shows a banner while changes are waiting, and approving applies all operations atomically. Applied settings changes can be reverted from Activity, History. Approvers editing directly in the Settings tab still apply immediately (audited).

## Look and feel

The theme is a black hole with an accretion disk. The starfield and the disk's particles (orbit, speed, colour) are generated by the Rust engine (`engine/src/cosmos.rs`, WebAssembly in the browser, TypeScript twin checked against `engine/testdata/cosmos_cases.json`); the browser only draws and animates them on a canvas behind the app. It stands still when the system asks for reduced motion.

The disk also shows the state of the review queue: it spins up and brightens while changes wait for you, and flares once when a change is applied. Status colours come from the same picture: blue-white for applied, orange for waiting, deep red for rejected or failed, always next to an icon and a word.

**Design system.** Every colour, type, space and motion value is a CSS variable in `frontend/app/tokens.css`; a test (`lib/contrast.test.ts`) fails if a text and background pair drops below WCAG AA. Fonts are self-hosted (Chakra Petch for titles, IBM Plex Sans and Sans Thai for text, IBM Plex Mono for SQL), so a build needs no font server. Shared parts live in `frontend/components/ui/`.

**Getting around.**

- A side rail groups the views (Work, Quality, Review, System). It shrinks to icons on a medium screen and becomes a bottom bar with a More sheet on a phone.
- The address bar holds your place (`/settings`, `/data`, `/database/master/province`), so refresh, back and shared links work.
- The Database tab lives at `/database/<db>/<table>`, and `?tab=fields|relations|erd|overview` picks the section. A hand-typed address that names nothing shows "There is no database …" or "There is no table …" instead of failing. On the ERD, click a table (or press Enter) to highlight its relations and open a panel with *Open fields*; Escape clears it, and *Fit to width* scales the diagram to the screen.
- `Ctrl K` (`⌘ K` on a Mac) opens the command palette: jump to any view or table, or run an action. `Shift Enter` on a table opens it in Data.
- Changes that need your decision arrive as a toast and collect in the review tray (`g` then `r`, or the Review button). The tray sits beside the page, so nothing covers your work. A change you just made opens the tray at once.
- Loading shows on the part that loads (a thin bar on the grid, skeleton rows on first load). Nothing blocks the whole app.

Screenshots: [`docs/superpowers/screens/2026-11-10/`](docs/superpowers/screens/2026-11-10/).

## Schema universe (3D)

The Schema tab shows each table as a body (bigger for more rows) and each foreign key as a link, laid out by a deterministic force-directed algorithm in the Rust engine (`engine/src/layout.rs`; WebAssembly in the browser, with a TypeScript twin checked against the same golden cases in `engine/testdata/layout_cases.json`). Drag to orbit, scroll to zoom, click a table (or pick it from the list) to see its columns and relations. The **2D** button shows the classic card diagram; it is also used automatically when the browser has no WebGL.

Not verified in the build sandbox: the 3D rendering itself (there is no GPU or browser here); the tests cover the layout, the toggle, selection and the fallback.

## Run it

```sh
cp .env.example .env            # fill in passwords and Google OAuth credentials
$EDITOR singularity.yaml        # set allowedDomains and seedAdmins
sh nginx/gen-dev-cert.sh        # self-signed cert for local HTTPS
docker compose up --build
```

Open https://localhost. nginx is the only service that publishes ports; everything else is on an internal network.

Google OAuth: create an OAuth client and add `https://localhost/api/auth/google/callback` as an authorized redirect URI.

## Roles

| Role | Can |
|---|---|
| viewer | read |
| editor | read, edit data |
| approver | read, edit, approve schema changes, manage users |

Domains allowed to sign in and the first admins come from `singularity.yaml`; roles are managed afterwards through the API (a Settings page comes in a later milestone).

## Develop

Engine (Rust; needed by the backend's duplicate finder):

```sh
cd engine
cargo test
pip install maturin && maturin build --release --out /tmp/wheels && pip install /tmp/wheels/*.whl
# optional browser build: wasm-pack build --release --target web --out-dir ../frontend/public/engine --features wasm
```

Backend (needs a local Postgres):

```sh
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export DB_HOST=localhost DB_APP_USER=postgres DB_APP_PASSWORD=... DJANGO_DEBUG=1
python manage.py migrate
pytest
# also run the DB role/permission tests (needs a superuser):
TEST_PG_ADMIN_USER=postgres pytest   # full suite, incl. real-role and MCP end-to-end tests
```

Frontend:

```sh
cd frontend
npm install
npm test && npm run typecheck && npm run build
```

Compose safety check (only nginx exposed): `python3 scripts/check-compose.py`
