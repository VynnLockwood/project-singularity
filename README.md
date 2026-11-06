# Singularity

An internal tool for designing and managing a **master data** database in PostgreSQL through prompts. AI clients (a built-in web chat via OpenRouter, plus external MCP clients such as Claude Desktop or Cursor) change the database only through an MCP server, and every change passes a configurable rules gate and an approval flow.

- Design: [`docs/superpowers/specs/2026-11-05-singularity-design.md`](docs/superpowers/specs/2026-11-05-singularity-design.md)
- Milestone 1 plan: [`docs/superpowers/plans/2026-11-05-milestone-1-foundation.md`](docs/superpowers/plans/2026-11-05-milestone-1-foundation.md)

## Status

Milestones 1 to 7 are done: compose stack, nginx gateway, Postgres schemas and roles, Google sign-in, roles, audit log, the rules gate, structured operations and SQL generation, the change lifecycle with approvals, backups and revert, REST API, and the MCP server. The `worker` container is a placeholder until later milestones. Milestone 7 (3D universe) turns the Schema tab into a 3D view with a 2D toggle. Milestone 6 (Rust engine) adds the Duplicates tab and column stats. Milestone 5 (grid editor) adds the Data tab. Milestone 4 (search) adds the embed service and schema search. Milestone 3 (web chat) adds the signed-in app: a chat with the assistant (OpenRouter), an Approve / Reject popup that appears live over WebSocket, a review queue, and a 2D schema diagram.

## Web chat

Set `OPENROUTER_API_KEY` in `.env` (only the `api` container receives it) and pick models in `singularity.yaml` under `chat` (`defaultModel`, `allowedModels`, `maxToolRounds`, `messagesPerMinute`). The assistant has the same tools as an MCP client for your role, but no approve tool: the popup is the only way to approve from chat, so the model cannot approve its own proposals. Changes proposed from any source (chat, MCP client) show up live for the requester and for approvers.

## Connect an AI client (MCP)

Sign in, open the **Connect** tab, name the connection and press **Create connection link**. You get, once, ready-to-paste settings for each client (only a hash of the token is stored):

- **Claude** (claude.ai, Desktop, mobile): Settings, Connectors, Add custom connector, paste the link `https://<your-host>/mcp/t/sgl_...`.
- **Gemini CLI**: the `mcpServers` JSON for `~/.gemini/settings.json` (URL plus an `Authorization: Bearer` header).
- **Claude Code**: the `claude mcp add --transport http ...` command.

The link form puts the token in the address because those connectors cannot send a header; it can appear in server logs, so revoke links you stop using (also in the Connect tab). Clients that can send headers should use `https://<your-host>/mcp` with `Authorization: Bearer sgl_...`. A client gets the same tools as your role, and every change still goes through the rules gate and approvals. Real OAuth 2.1 sign-in for connectors is a planned follow-up.

## Data grid

The Data tab edits one table at a time: server-side paging, sort and filter, reference columns as dropdowns (labelled by the target's name column), and a draft that collects your edits. **Save for approval** sends one proposal through the same rules gate as everything else: identical edits are grouped into one `update_rows`, new rows into one `insert_rows`, and removed rows are *deactivated* (`isActive = false`), never deleted. Editors approve their own data changes in the popup. Viewers see the grid read-only.

Limits: the grid does not detect that someone else changed the same cell after you loaded the page (last approved write wins), and one save is capped by `maxOpsPerProposal` / `maxRowsPerDataOp` in `singularity.yaml`.

## Search

Ask "where does this data belong?" in the Schema tab (Thai or English), or let the assistant call `search_schema`. Every table and column becomes a document (name, type, comment, relationships, a few sample values) embedded by the `embed` container (multilingual-e5-small, CPU, no internet at runtime: the model is baked into the image at build). Ranking blends meaning (pgvector, cosine) with name similarity (`pg_trgm`), and an exact name always ranks first. The index refreshes in the background after an applied schema change; approvers can rebuild it. If nothing scores above `search.minConfidence` (default 0.8), the UI offers to ask the assistant to propose a new column. That threshold was set without running the real model, so tune it against your own schema.

Known gaps: the Settings UI (rules, roles, default model) is a later milestone; MCP auth uses personal access tokens rather than OAuth 2.1 (planned follow-up), and there is no scheduled nightly backup yet (a backup is taken before every schema change).

## Duplicates and column stats

The `engine/` crate (Rust) does the heavy lifting twice from one codebase: as a Python module (PyO3) inside the backend for duplicate detection, and as WebAssembly in the browser for column statistics. Names are normalized (Unicode NFKC, Thai digits and zero-width marks, company suffixes like Co., Ltd., จำกัด), grouped by blocking keys, then scored with Jaro-Winkler, token-sorted Jaro-Winkler and bigram overlap; a group's score is its weakest link.

The **Duplicates** tab scans one table at a time (`duplicates.*` in `singularity.yaml`), lists groups with their score, and shows the records side by side. Pick the record to keep and **Merge**: this is an ordinary data change that moves every foreign-key reference to the survivor and then deactivates the others, so it goes through the same rules gate and approval popup. "Not duplicates" dismisses a group and a scan will not bring it back.

The **Column stats** button in the Data tab summarises the rows on screen (distinct, empty %, min/max/mean, top values). The browser build is optional: if `frontend/public/engine/` is missing, a TypeScript implementation that passes the same golden test cases (`engine/testdata/stats_cases.json`) is used. CI builds the WASM package and uploads it as an artifact.

Not verified in the build sandbox: the Docker image (Rust stage), the WASM build, and real browser behaviour.

## Look and feel

The theme is a black hole with an accretion disk. The starfield and the disk's particles (orbit, speed, colour) are generated by the Rust engine (`engine/src/cosmos.rs`, WebAssembly in the browser, TypeScript twin checked against `engine/testdata/cosmos_cases.json`); the browser only draws and animates them on a canvas behind the app. It stands still when the system asks for reduced motion.

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
