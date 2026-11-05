# Singularity

An internal tool for designing and managing a **master data** database in PostgreSQL through prompts. AI clients (a built-in web chat via OpenRouter, plus external MCP clients such as Claude Desktop or Cursor) change the database only through an MCP server, and every change passes a configurable rules gate and an approval flow.

- Design: [`docs/superpowers/specs/2026-11-05-singularity-design.md`](docs/superpowers/specs/2026-11-05-singularity-design.md)
- Current milestone plan: [`docs/superpowers/plans/2026-11-05-milestone-1-foundation.md`](docs/superpowers/plans/2026-11-05-milestone-1-foundation.md)

## Status

Milestone 1 (Foundation) in progress. Done: compose stack, nginx gateway, Postgres schemas and roles, Django skeleton, Google sign-in, roles, audit log, Next.js skeleton. The `mcp`, `worker` and `embed` containers are placeholders until later milestones.

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
TEST_PG_ADMIN_USER=postgres pytest tests/test_db_roles.py
```

Frontend:

```sh
cd frontend
npm install
npm test && npm run typecheck && npm run build
```

Compose safety check (only nginx exposed): `python3 scripts/check-compose.py`
