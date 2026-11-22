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

Every tool states the role it needs; a client only sees the tools its user's role allows. The web chat assistant has the same tools, except the two that decide a change and the two MCP-only import helpers (`match_data`, `get_import_recipe`).

| Tool | Needs | What it does |
|---|---|---|
| `get_rules` | viewer | The team's design rules. |
| `list_schemas`, `list_tables`, `describe_table` | viewer | Databases, tables, columns. |
| `search_schema` | viewer | Find where a kind of data belongs (Thai or English). |
| `suggest_query` | viewer | Turn plain-language comments into one checked, read-only SELECT. It does not run it. |
| `match_data`, `get_import_recipe` | editor | MCP only (not in the web chat). For a spreadsheet the client already read: the local Profiler and Matcher's view of its headers and a sample (up to 5,000 rows and 100,000 cells, each cut to 500 characters): column types, best tables and columns, refs, rows that already exist; and the approved recipe for that file shape. Nothing sent is stored. |
| `read_rows` | viewer | One table, a page at a time, with sort and filters. No SQL needed. |
| `query` | viewer | One read-only `SELECT`. |
| `export_schema` | viewer (data: editor) | A database as SQL, inline up to `limits.maxInlineExportBytes`. |
| `list_changes`, `get_change` | viewer, editor | Change history and one change's diff. |
| `list_duplicates`, `get_duplicate` | viewer | Duplicate groups and their records side by side. |
| `get_settings` | viewer | Quality settings and every gate rule. |
| `propose_change` | editor | Structure and data changes as gated operations. |
| `propose_config_change` | editor | Settings, rules and roles. Always waits for an approver; a role change waits for an admin. |
| `recheck_change` | editor | Run the gate again on a stale change. |
| `find_duplicates`, `merge_duplicates`, `dismiss_duplicate` | editor | Scan a table, merge a group (a data change), or mark it as not duplicates. |
| `submit_script`, `validate_script`, `get_script` | editor | Load a pg_dump-style SQL script as gated operations. |
| `begin_load`, `append_rows`, `finish_load`, `get_load`, `cancel_load` | editor | Load thousands of rows (up to `limits.maxScriptRows`, default 200,000) into an existing table from chat, no shell needed. |
| `approve_change`, `reject_change`, `list_pending` | approver | Decide a change, after the user confirms. |
| `revert_change` | approver | Undo an applied change that has a stored reverse. |
| `get_audit_log` | approver | Who did what and when. |
| `list_users` | approver | People and their roles. |
| `reindex_search` | approver | Refresh the schema search index. |
| `get_user_access`, `set_user_access` | admin | Read or replace one person's per-database grants (applied at once, audited as `access.changed` with `via: "mcp"`; not offered in the web chat). |

## Raw SQL

**Script upload (web).** The Scripts tab takes a pg_dump-style file of up to `limits.maxScriptBytes` (default 512 MiB; the tunable goes up to 2 GiB, and it is shown on the page). Choosing a file is the whole flow: the browser sends it in 8 MiB parts (a failed part is retried and resumed, an unfinished upload expires after 24 hours), the server checks it in the background (reading, parsing, checking rows, dry run, with a progress line) and the page says "Ready: N tables, M rows" with a Submit button. Statements that are harmless or cannot be gated are skipped instead of failing the file and listed under **Not imported, and why**: extensions, sequences and their `nextval` defaults, grants, and functions, triggers and policies (that last group is shown as a warning, because the logic will be missing). `DROP`, `DELETE`, `UPDATE`, `TRUNCATE` and `ALTER TABLE ... DROP` still fail with a plain explanation. **Customize (optional)** holds the destination picker and the mapping JSON; changing it re-runs the check once. API users: `POST /api/scripts/uploads` then `PUT /api/scripts/uploads/<id>/<n>` and `POST .../complete`, `GET /api/scripts/limits` for the cap and chunk size, and `POST /api/scripts/<id>/validate?async=1` (202, then poll `GET /api/scripts/<id>` until `report.state` is `done` or `failed`; a check that stops answering for 10 minutes reads as failed). A check holds the whole file as text in memory (about 2.5 times its size, more with emoji or other 4-byte characters), so at most two run at once; size the api container for that. Each user can have 5 open uploads.

**What an AI client can do.** A chat-only client can do every database task the web app can: read and search, propose schema, data and configuration changes (including `rollback_rule` to undo a rule change, seen with `get_rule_history`), find, merge, dismiss and `reopen_duplicate` groups, look at a column with `profile_column`, find ids to reference with `lookup_options`, and (approvers) `create_backup` and `list_backups`. What stays with a person: approving in the web chat (it has no approve tool), restoring a backup, signing in, and creating or revoking access tokens. `backend/tests/test_mcp_parity.py` fails when a new web `api/` route has neither an MCP tool nor a stated reason it is web-only.

**Loading big data over MCP.** `insert_rows` is for a few hundred rows. For more, an AI client calls `begin_load(table, columns)`, then `append_rows(load_id, seq, csv)` once per chunk of about 2,000 rows (header-less CSV; an unquoted empty field is NULL, `""` is the empty string; a failed call is retried with the same `seq`), then `finish_load(load_id)`. Nothing touches the database until `finish_load`, which runs the same checks as a script upload (rules gate, foreign keys, a rolled-back dry run) and submits one approvable change; undoing it deactivates the loaded rows. Optional `refs` fill a foreign key from a parent business key (districts by province code). Open loads expire 24 hours after their last chunk; limits are tunable in Settings (`limits.maxLoadChunkBytes`, `limits.maxLoadChunkRows`, `limits.maxLoadBytes`, `loads.ttlHours`, `loads.maxOpenPerUser`).

**Importing a spreadsheet over MCP.** The AI client is the designer: it reads the file itself, calls `match_data(schema, headers, rows)` with a sample (and `get_import_recipe`) to learn which tables, columns and rows already exist, designs the mapping, then loads through the gate with `begin_load`/`append_rows`/`finish_load` or `submit_script`. `match_data` uses the same local services as the Import tab (profiler, multilingual-e5 column matching with a by-name fallback when search is offline, duplicates engine) and never stores the rows. Both tools need the editor role and are offered to MCP clients only; in the web app a file goes through the Import tab.

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

## Intelligent query

The **Intelligent query** tab (any signed-in user) is a SQL editor that suggests a query from a plain-language comment, like autocomplete. Write what you want on a `--` line, such as `-- contracts of partner Acme`, and press Enter. A gray suggestion appears at the cursor: **Tab** takes it, **Esc** drops it, **Ctrl+Enter** runs the statement under the cursor (or the selection). Add another comment later, such as `-- and status still open`, and the new suggestion builds on the earlier comments and the SQL already written. The text is kept per database in your browser.

How it works: the `embed` container finds the tables that fit the comments (plus their foreign-key neighbours), the tool looks up real values in those tables that match words in your comment (so "Acme" becomes the exact stored name), then the chat model writes one SELECT. Every suggestion is checked before you see it: one statement, SELECT or WITH only, and the database must be able to plan it read-only; one retry if it is not. Suggestions never run by themselves.

The team's memory: a query you accepted and ran successfully is saved as a candidate and becomes a team example after a second person (or a second day) confirms it. Active examples are shown to the model as guidance, and a close match for the same question and the same stored values is offered instantly "from memory". Examples whose SQL no longer plans (a column was renamed) are deleted. Approvers can list and delete examples at `GET`/`DELETE /api/intelligent-query/examples`. AI clients use the `suggest_query` tool.

It needs the `embed` service for table search (without it, tables are matched by name and the tab says so) and a chat model: `intelligentQuery.model` in `singularity.yaml`, else the chat default. Your own API key from the Chat settings is used if you set one. Other settings: `maxTables`, `examples`, `memoryHit`, `ratePerMinute` (suggestions per user per minute), `valueLookupMs`. Your comments and the table and column names (not row data beyond the matched values) are sent to the chat provider.

## Import

The **Import** tab (editors and approvers) loads an `.xlsx` or `.csv` file into the database with the help of four roles that hand work to each other. You upload the file, optionally write what you want ("skip inactive vendors"), and watch the thread as each role reports.

- **Profiler** (local) reads every sheet and describes each column: type, empty share, distinct values, patterns, and a few sample values.
- **Matcher** (local) uses the `embed` container (multilingual-e5-small) to find the tables and columns that fit each sheet and header, which columns are references to existing records, and which rows already exist or may exist (`import.rowMatch`). If the `embed` service is offline it says so and falls back to name matching.
- **Designer** (the chat model) proposes the mapping: target table per sheet, which header fills which column, what to drop, references to existing parents, filters, and a one-line reason for each decision. It may ask up to 3 questions first; the run then waits in `waiting_for_answer` until you answer.
- **Critic** (local) turns the plan into a script and runs the usual validation: a gate dry run, orphan references, bad rows with their line. It reports new rows, rows that already exist (skipped), rows removed by the filter, and problems.

The relay: `profiling`, `matching`, `designing`, `checking`, then `ready`. Rows that already exist are found by the key the final plan loads (the target's unique column and the header mapped to it), not only by the Matcher's first guess. If the Critic finds problems, they go back to the Designer as feedback, for at most `import.maxRounds` rounds (default 3); after that the run is `failed` and says why. You can edit the plan, skip rows, and recheck (the Critic only). `submitted` means the change waits for an approver. A run that stops reporting for 10 minutes is marked failed.

What is sent to the chat model: the column profiles with up to 5 distinct sample values per column (the first distinct values in file order; `import.sampleValues`, default 5, never above 5; each cut to 80 characters), the Matcher's candidates and counts (how many rows already exist or may exist), the team's enabled rules, the text of the candidate target tables (keys, columns, types, foreign keys, comment), your prompt, your answers, the Critic's feedback, and a recipe if one applies. The Critic's feedback names only the column, constraint or table and a row count: the database's detail (the failing row, the duplicate key value, an orphan's value) stays in the thread you see and is never sent. Full rows are never sent, and neither are the matching rows from existing records; matching is done by the local embedding model. The model is `import.model`, else the chat default.

Recipes: a file whose headers (over all sheets, trimmed and lower-cased, order ignored) match an earlier import in the same database starts from the mapping and filters approved then. The recipe is saved when the change is applied, not when it is submitted. If it no longer fits the database (a column was renamed), the Critic reports the drift and the Designer repairs it.

API key: sent with the request that starts or resumes the run (the key typed on the Import tab, else your own key from the Chat settings, else the configured provider key) and handed to the background process through the environment variable `SINGULARITY_IMPORT_KEY`. It is never stored in the database or returned by the API.

Limits and settings (`import.*` in `singularity.yaml`, all optional): `model` (empty = chat default), `maxFileBytes` (default 50 MB), `sampleValues` (5), `maxRounds` (3), `rowMatch` (true). Rows read per sheet are capped by `limits.maxScriptRows`. A run, its file and its thread are deleted after 24 hours unless submitted. One open run per person per database; starting another one offers a link back to the open run.

The load is one gated change: nothing is written until an approver approves it, and it goes through the same rules, diff and audit log as any change. A file with nothing new to load (every row already exists, is filtered out or skipped, and no table or column is added) ends `failed` at once with "Nothing new to load: …" and the counts; the Designer is not asked again. Edit the filters or the skipped rows and recheck if that is not what you meant.

Import is web-only: AI clients have no tool for it because they load data themselves with `begin_load`, `append_rows` and `finish_load`.

## Duplicates and column stats

The `engine/` crate (Rust) does the heavy lifting twice from one codebase: as a Python module (PyO3) inside the backend for duplicate detection, and as WebAssembly in the browser for column statistics. Names are normalized (Unicode NFKC, Thai digits and zero-width marks, company suffixes like Co., Ltd., จำกัด), grouped by blocking keys, then scored with Jaro-Winkler, token-sorted Jaro-Winkler and bigram overlap; a group's score is its weakest link.

The **Duplicates** tab scans one table at a time (`duplicates.*` in `singularity.yaml`), lists groups with their score, and shows the records side by side. Pick the record to keep and **Merge**: this is an ordinary data change that moves every foreign-key reference to the survivor and then deactivates the others, so it goes through the same rules gate and approval. "Not duplicates" dismisses a group and a scan will not bring it back.

The **Column stats** button in the Data tab summarises the rows on screen (distinct, empty %, min/max/mean, top values). The browser build is optional: if `frontend/public/engine/` is missing, a TypeScript implementation that passes the same golden test cases (`engine/testdata/stats_cases.json`) is used. CI builds the WASM package and uploads it as an artifact.

Not verified in the build sandbox: the Docker image (Rust stage), the WASM build, and real browser behaviour.

## Settings

The **Settings** tab has three sections. **Quality**: search confidence, duplicate similarity, scan caps, query and proposal limits, chat model and rate limits, backup retention. Approvers change them and they apply within seconds (stored in the database over the values in `singularity.yaml`; Reset returns to the file's value; every change is audited). **Rules**: turn each gate rule on or off, switch it between block and warn, edit its parameters, see its history and restore an older version. **Team** (approvers): who to ask about roles; admins manage people on the Users page (see Access). Deploy-time things (the people allowed to be invited by domain, the list of allowed models, the embedding model) stay in `singularity.yaml`.

**Changing settings from an AI client.** Settings, rules and roles can also be changed over MCP and in the web chat, but never directly: `get_settings` (viewer) reads them, `list_users` (approver) lists people with their roles, and `propose_config_change` (editor) proposes operations (`set_setting`, `reset_setting`, `update_rule`, `create_rule`, `set_user_role`; up to 20, not mixed with schema or data operations). The proposal is dry-run in a rolled-back transaction, then waits in the approval queue as a **Settings change**. Approvers (not the proposer) see it live in the review tray and Activity (a change that includes `set_user_role` can only be approved or rejected by an admin), the Settings tab shows a banner while changes are waiting, and approving applies all operations atomically. Applied settings changes can be reverted from Activity, History. Approvers editing directly in the Settings tab still apply immediately (audited).

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
$EDITOR singularity.yaml        # set seedAdmins and allowedDomains (ships with example.com placeholders)
sh nginx/gen-dev-cert.sh        # self-signed cert for local HTTPS
docker compose up --build
```

Set `DJANGO_SECRET_KEY` in `.env` (the app refuses to start without a real one); behind a real host, also set `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`.

Open https://localhost. nginx is the only service that publishes ports; everything else is on an internal network.

Google OAuth: create an OAuth client and add `https://localhost/api/auth/google/callback` as an authorized redirect URI.

## Access

Sign-in is invite-only. An admin adds a person, and only then can that person sign in with Google. An unknown, turned-off or unverified Google account is refused on the web, and also when an AI client signs in over MCP OAuth or uses a token.

| Role | Can |
|---|---|
| viewer | read |
| editor | read, propose data and schema changes (editors approve their own data changes, in the web app only) |
| approver | read, edit, approve, reject and revert normal changes, change settings; over MCP also `list_users`, `get_audit_log`, `list_pending`, `revert_change`, `create_backup`, `list_backups`, `reindex_search` |
| admin | everything an approver can, plus manage users, approve or revert role changes, and manage per-database access (Users page, or `get_user_access` / `set_user_access` over MCP) |

**Adding people and changing roles.** Admins open the **Users** page (`/users`; other roles get a not-found page). There they add a person by email with a role, change a role, or turn access off and on again. You cannot turn yourself off, and the last active admin cannot lose the role or be turned off. Roles can also be proposed with `set_user_role` from an AI client or the chat, but the change waits until an admin approves it; reverting a role change also needs an admin. A role change or turning someone off closes their open live-update connection, so the browser reconnects under the new role. The audit log (`/api/audit`, `get_audit_log`) is for approvers and admins.

**First admins.** `auth.seedAdmins` in `singularity.yaml` lists emails. On every migrate any missing seed is created as an active admin. If no real active admin exists, existing seed users who are active are promoted to admin; a seed admin that is still listed and has never signed in does not count as real, so a mistyped seed cannot block this. Seeds on reserved domains (`example.com`, `example.org`, `example.net`, `*.invalid`, `*.test`, `localhost`) are skipped with a warning. A turned-off user is never turned back on by this, and a warning is logged when no active admin exists afterwards. A seeding error is logged and never stops the migration. `auth.allowedDomains` only limits which email domains an admin may invite; empty means any domain. It does not decide who can sign in.

**Database access.** Roles are capped per database: a grant gives a person viewer, editor or approver on one database (or `*`, all of them), never above their role; admins reach every database. A database someone cannot reach answers exactly like one that does not exist. Editors can no longer propose `create_schema`: a new database needs a global approver (or admin), who gets approver on it. SQL cannot get around grants: `query`, the Intelligent query tab and `suggest_query` refuse SQL that names, or whose plan reads (views included), a database the person cannot read, and refuse reading the system catalogs (`pg_namespace`, `pg_class`, `pg_tables`, other `pg_*` catalogs, `information_schema`) and refuse every system function that takes an object id or a `reg*` value, or turns ids into names (such as `regclassout`, `oidvectortypes`, `format_type`, `obj_description`, `pg_get_viewdef`). A few functions that return a `reg*` value stay allowed (`pg_typeof`, `get_current_ts_config`, `to_regclass` and the other `to_reg*`). A name given as a string constant, as in `nextval('master.s')` or `'t'::regclass`, is checked like any other name when it is a plain, optionally qualified name; arrays of names, argument lists such as `'f(int)'::regprocedure`, numbers and other shapes are refused for non-admins. Because Postgres can silently turn any string constant next to a `reg*` expression into that type (`COALESCE('master.t'::regclass, 'crm.t')`, `x IN ('a', 'crm.e')`), a statement holding any `reg*` expression (a cast, one of those functions, a `reg*`-typed column or a user function returning one) must have every string constant be a plain name, checked as a name; so a non-admin cannot combine, say, `'master.t'::regclass` with `name = 'Bob Smith'` in one statement (split it, or drop the cast). reads an object named in a text argument (`pg_input_is_valid`, `pg_input_error_info`, `currtid2`), runs SQL text (`query_to_xml`, `ts_stat`) or changes `search_path` (`set_config`); every other catalog function that takes text was reviewed as never reading it as an object name, and text search configurations and dictionaries are accepted only as constants checked like names. Admins may read the catalogs. Raw SQL (`propose_sql`), view queries and scripts may name only databases where the proposer is editor, and may not read a view, inheritance child or partition whose data comes from another database, checked before any dry run. Every expression of a plpgsql body is checked, assignment targets included (`a[(SELECT ...)] := 1`); a plpgsql statement kind that was not reviewed refuses the body, and so does a body the plpgsql parser cannot read (for example one declaring `v master.t%ROWTYPE` and then assigning `v.name := ...`). Trigger arguments that look like qualified names are checked as names, and catalog trigger functions other than `suppress_redundant_updates_trigger` (such as `tsvector_update_trigger`, whose arguments name a configuration) are refused for non-admins. A shape that cannot be checked is refused for everyone but admins: dynamic `EXECUTE`, a plpgsql declaration naming another database's type, and statements that name functions outside a call (`CREATE AGGREGATE`, `CREATE OPERATOR`, `CREATE TYPE`, `CREATE CAST`, text search objects, statistics, operator classes and the like), so only an admin can create those. Reading a view (or using an operator) also follows what it is built on: functions, operators, types, casts and text search configurations of other databases, and risky catalog functions inside it, are refused, on the `query` path too. Raw SQL may not change `search_path`, the role or the run's limits with SET/RESET or `set_config` (named in any position, including an aggregate's state function), also not inside a DO block or function; this holds for admins too, but admin raw SQL is otherwise trusted (dynamic `EXECUTE` could still build such a SET). This also refuses the hardening pattern `CREATE FUNCTION ... SECURITY DEFINER SET search_path = master, pg_temp` in raw SQL, for everyone (as it always did); such functions are created outside Singularity by a database administrator. Behind the specific checks is a generic one: any qualified name in a statement position the checker does not know is refused for non-admins, and a corpus of more than 250 statement shapes (`backend/tests/test_access_isolation_corpus.py`) tests that each answers the same for a hidden database as for a missing one. Raw SQL is checked again against the proposer's access when it is approved (editor on the database it runs in, and every rule above); if they lost that access or it would now reach further, the change goes stale. **Known gap (accepted):** a function in a readable database that itself reads another database is not detected: `SELECT f()` shows only the call. Such a function can only be written by someone who is editor on both databases (or an admin), but once it exists anyone who can read its database can call it. The same goes for triggers and column defaults that call such a function. Related accepted limits: a plpgsql function in a readable database that uses unqualified names resolves them under the caller's `search_path`, and a function an admin writes around a risky catalog function is not looked into (function bodies are not followed).

**AI clients.** A token acts as its user's real role on every call, so a role change or turning a user off takes effect at once. Approving over MCP checks the approver's role, and role changes need an admin. Raw SQL can only be approved in the web app.

**Profile.** Everyone has a **Profile** page (`/profile`) that shows what they can do on the web and in an AI client.

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

## License

[MIT](LICENSE)
