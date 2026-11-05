# Singularity

An internal tool for designing and managing a **master data** database in PostgreSQL through prompts. AI clients (a built-in web chat via OpenRouter, plus external MCP clients such as Claude Desktop or Cursor) change the database only through an MCP server, and every change passes a configurable rules gate and an approval flow.

- Design: [`docs/superpowers/specs/2026-11-05-singularity-design.md`](docs/superpowers/specs/2026-11-05-singularity-design.md)
- Milestone 1 plan: [`docs/superpowers/plans/2026-11-05-milestone-1-foundation.md`](docs/superpowers/plans/2026-11-05-milestone-1-foundation.md)

## Status

Milestones 1 to 4 are done: compose stack, nginx gateway, Postgres schemas and roles, Google sign-in, roles, audit log, the rules gate, structured operations and SQL generation, the change lifecycle with approvals, backups and revert, REST API, and the MCP server. The `worker` container is a placeholder until later milestones. Milestone 4 (search) adds the embed service and schema search. Milestone 3 (web chat) adds the signed-in app: a chat with the assistant (OpenRouter), an Approve / Reject popup that appears live over WebSocket, a review queue, and a 2D schema diagram.

## Web chat

Set `OPENROUTER_API_KEY` in `.env` (only the `api` container receives it) and pick models in `singularity.yaml` under `chat` (`defaultModel`, `allowedModels`, `maxToolRounds`, `messagesPerMinute`). The assistant has the same tools as an MCP client for your role, but no approve tool: the popup is the only way to approve from chat, so the model cannot approve its own proposals. Changes proposed from any source (chat, MCP client) show up live for the requester and for approvers.

## Connect an AI client (MCP)

Endpoint: `https://<your-host>/mcp`. Authenticate with a personal access token sent as `Authorization: Bearer sgl_...`.

1. Sign in, then create a token with `POST /api/mcp-tokens` (it is shown once; only a hash is stored).
2. Add the server to your client, e.g. Claude Desktop / Cursor: URL above plus the bearer header.

Tools are filtered by your role: viewers get `get_rules`, `list_tables`, `describe_table`, `query` (SELECT only); editors add `propose_change`, `get_change`, `approve_change`, `reject_change`; approvers add `list_pending`. The AI proposes a change, shows you the diff and SQL, and only after you say yes does it call `approve_change`. Data edits can be approved by the editor who made them; schema changes need an approver.

## Search

Ask "where does this data belong?" in the Schema tab (Thai or English), or let the assistant call `search_schema`. Every table and column becomes a document (name, type, comment, relationships, a few sample values) embedded by the `embed` container (multilingual-e5-small, CPU, no internet at runtime: the model is baked into the image at build). Ranking blends meaning (pgvector, cosine) with name similarity (`pg_trgm`), and an exact name always ranks first. The index refreshes in the background after an applied schema change; approvers can rebuild it. If nothing scores above `search.minConfidence` (default 0.8), the UI offers to ask the assistant to propose a new column. That threshold was set without running the real model, so tune it against your own schema.

Known gaps: the Settings UI (rules, roles, default model) and 3D view are later milestones; MCP auth uses personal access tokens rather than OAuth 2.1 (planned follow-up), and there is no scheduled nightly backup yet (a backup is taken before every schema change).

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
