# Milestone 1: Foundation — Implementation Plan

**Spec:** `docs/superpowers/specs/2026-11-05-singularity-design.md` (section 8, milestone 1)
**Goal:** a running Docker stack where nginx is the only exposed port, Django and Next.js respond, Postgres has the `singularity` and `master` schemas with the three DB roles, and users can sign in with Google and get a role.

## Scope
In: compose stack, nginx routing, Postgres + pgvector init, Django project and apps, Next.js skeleton, Google OAuth with domain restriction, roles (viewer/editor/approver) with YAML admin seed, tests.
Out (later milestones): rules engine, MCP tools, chat, search, grid, Rust engine, 3D UI. The `mcp`, `worker` and `embed` containers exist in compose but run placeholders.

## Tasks

### 1. Repo scaffold and config
- Directories: `frontend/`, `backend/`, `engine/` (empty placeholder), `nginx/`, `db/init/`.
- `.env.example`, `singularity.yaml` (allowed domains, seed admins, default role, limits stub), `.gitignore`, `README.md`.
- **Done when:** layout matches spec section 1; no secrets committed.

### 2. Postgres init
- `db/init/01-schemas-roles.sql`: create schemas `singularity` and `master`; roles `singularity_app`, `singularity_master_rw`, `singularity_master_ro` with the grants from spec section 1; `CREATE EXTENSION vector, pg_trgm`.
- Passwords come from env via a shell wrapper in the init step.
- **Test:** script connects as each role and asserts what each can and cannot do (ro cannot write `master`; rw cannot touch `singularity`; app cannot write `master`).

### 3. Django backend skeleton
- Project `config`, apps `accounts`, `settings_app`, `audit` (empty app stubs for later milestones).
- `/api/health` returns DB connectivity.
- Settings read `.env` and `singularity.yaml`.
- **Test:** health endpoint test with a test Postgres.

### 4. Auth and roles
- Google OAuth (authorization code flow) via `django-allauth` or `social-auth` limited to the allowed domain(s).
- `accounts.User` with `role` field; seed admins from YAML on first login; default role from YAML.
- Role permission helpers: `can_read`, `can_edit`, `can_approve`.
- Settings API (approver only) to change a user's role, audited in `audit`.
- **Tests:** domain restriction, seed admin gets approver, default role, role change requires approver, role change is audited.

### 5. Next.js skeleton
- App Router, TypeScript, dark space theme tokens, i18n scaffold (en/th).
- Pages: sign-in, home placeholder (shows user and role), 403 page.
- Calls `/api/me`.
- **Test:** component test for the role-aware home page.

### 6. Compose and nginx
- `docker-compose.yml`: nginx, web, api, mcp (placeholder), worker (placeholder), embed (placeholder), db, redis; internal network; only nginx publishes 443/80.
- `nginx/nginx.conf`: `/` → web, `/api/` → api, `/ws/` → api (upgrade headers), `/mcp` → mcp; HTTP→HTTPS redirect; dev self-signed cert script.
- **Test:** `docker compose config` validates; script asserts only nginx publishes ports.

### 7. Verify and ship
- Run backend tests, frontend tests, compose validation.
- Commit in small commits as VynnLockwood; push `feat/foundation`.

## Done criteria for the milestone
- `docker compose up` starts the stack; `https://localhost/api/health` is OK through nginx; no other port is reachable from the host.
- A Google user in the allowed domain can sign in; others are rejected.
- Seed admin is approver; others get the default role; role changes are audited.
- All tests pass.
