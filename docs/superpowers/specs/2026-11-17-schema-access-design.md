# Per-database access (schema-level permissions) — design

## Intent

Roles are global today: any viewer can read every database (a "database" is a Postgres schema), and any editor can propose changes to any of them. That is fine for one trusted team, wrong once different groups share the tool. We want an admin to decide, per person, which databases they can reach and at what level, on the website and from an AI client, and have that enforced everywhere data or structure is read, searched, changed, approved or exported.

This supersedes the admin-management spec's non-goal "per-database permissions".

## Goals

- A grant is `(user, database) → viewer | editor | approver`. A user's **global role is the ceiling**: effective level = min(global role, grant). Admins reach every database, present and future, with no grants.
- Fail closed: a database a user has no grant for does not exist for them (hidden, see Visibility).
- Admin manages grants on the **website** (Users page: an Access panel per user) and over **MCP** (`get_user_access`, `set_user_access`, admin only, hidden from everyone else). Both call the same service; every change is audited and ends the target's live sockets so it applies at once.
- One function, `effective_role(user, schema)`, answers every question. All web endpoints, MCP tools, chat tools, search, duplicates, change history, approval queue, live events, scripts/loads, backups and export use it. No place decides on its own.
- Profile page and `/api/me` show "Your databases" and the level on each.
- Deploy never changes what anyone can do today (migration, below).

## Non-goals

- Table-level or column-level permissions (a later, lighter "restricted table" flag was discussed, not built here).
- Teams or groups, custom roles, per-permission matrices.
- Letting an AI client grant access without an admin's token. The MCP tools are admin-only; a non-admin token never sees them.
- Changing global roles, invite-only sign-in or approval rules for **config** changes (settings, rules, roles). Those stay global.

## Anti-goals

- No path where a user reads, searches, proposes against, approves, exports or backs up a database they have no grant for — including by naming it inside raw SQL, a `query` SELECT, a script, a load, a search `*`, a history or audit list, or a live event.
- A user must not learn that a hidden database exists: a database they cannot reach answers exactly like one that does not exist.
- A non-admin cannot grant or widen access for anyone, themselves included.
- Existing users are not locked out on deploy.

## Approaches considered

1. Allow-list per user at the global role. Simplest; cannot express "editor in crm, viewer in finance". Rejected.
2. **Role per database, capped by the global role (chosen).** Expresses real splits; one `effective_role` function; modest UI.
3. Teams that own databases. Most flexible; large for the need. Rejected for now.

## Design

### Model

`SchemaGrant(user FK, schema CharField(63), role)` with a unique constraint on `(user, schema)`. `schema` is a managed database name or the literal `*` ("all databases, including ones created later"). `role` ∈ viewer/editor/approver; `admin` is never a grant (admin is global).

Effective role of a non-admin for database S:

1. take the grant for S if there is one, else the `*` grant, else **none**;
2. cap it by the user's global role (grant above the cap is stored but does not exceed it);
3. an inactive user has none.

A specific grant overrides `*` in either direction (`*` viewer + `crm` editor; `*` editor + `finance` viewer). Admins: admin on everything. Validation on write: schema must be a managed database or `*`; role ∈ viewer/editor/approver; grants for an admin target are refused ("admins reach every database"); a grant above the target's global role is refused with an explanation (it could never take effect).

`accounts/access.py` (new) holds `effective_role`, `can_read/can_edit/can_approve(user, schema)`, `readable_schemas(user)`, `require(user, schema, min_role)` (raises the same `UnknownSchema` a non-existent database raises when the user has no access at all, and a normal forbidden when they can read but lack the level), and `affected_schemas(operations)` for changes.

### Deploy / migration

- Every existing **non-admin** user gets a `*` grant at their current global role. Behaviour is unchanged on deploy, and future databases stay reachable for them until an admin narrows it.
- New users created through `create_user` start with **no grants**; the Add-user form and the MCP tools can set grants at invite time (`access` optional).
- Seed admins are admins and need nothing.
- When a `create_schema` change is **applied**, its **proposer** (a global approver, see the table) gets an `approver` grant on the new database, capped by their global role; admins already have it. (Builder-check call: the proposer asked for the database, so the proposer keeps it; the approving person gets nothing automatically.) Grants follow `rename_schema`; `drop_schema` deletes that database's grants. All audited.

### Visibility

- `list_schemas` / `GET /api/schemas` return only databases the user can read, with table counts. Names, counts, comments of others are never returned.
- A direct request for an unreachable database fails with the same error text and status as a database that does not exist (`UnknownSchema`, 400 on the web; the same "Unknown schema … Call list_schemas" on MCP). No "forbidden" for a database the user cannot see at all.
- A user who can read a database but lacks the level for an action gets the existing role-style refusal.

### What each action needs

| Action | Needs on the database(s) involved |
|---|---|
| read tables, rows, graph, profile, lookups, `query`, search results, duplicates | viewer |
| propose data/schema changes, raw SQL, scripts, loads, import, merge duplicates | editor |
| approve, reject, revert, recheck a change; approve raw SQL; backups; reindex of that database | approver |
| export with data | editor (as today); structure only: viewer |
| `create_schema` | global approver (the new database does not exist yet); creator gets an approver grant |
| `drop_schema`, `rename_schema`, `move_table` | approver on **every** database involved |
| config changes (settings, rules, roles), tokens, user management | unchanged (global role) |

A change that touches several databases (`move_table`, `rename_schema`) is checked against `affected_schemas(operations)`: the proposer needs editor and the decider approver on every one. A self-approval path (editor approving their own data change where today's rules allow it) also needs editor on that database.

### Lists that span databases

These filter to readable databases **inside the query**, not after a top-k cut:

- search with `schema=*` (the indexer keeps indexing everything; the search query adds `schema IN (readable)`);
- duplicates with `*`; change history (`schema` absent = all); the approvals/pending queue; the audit log (entries that name a database are limited to ones the viewer can reach; entries without one behave as today);
- backups list, jobs/loads/scripts lists, intelligent query and chat tool results.

### Changes with no database (config, users, tokens)

Config changes (settings, rules, roles), user and token actions and their audit entries have no database. They behave exactly as today by **global role**: a global approver sees them in the queue and receives their events whatever their grants (a global approver with only viewer grants still sees and decides config changes). Only database-scoped items are filtered by grants. Live events therefore keep the existing global approver group for config events and add per-database groups for database-scoped ones. (Builder-check call.)

### Live events

Change events are delivered to sockets by database. A socket joins the groups of the databases its user can read (approver-level events only for databases where the user is approver). A grant change, role change or turn-off closes that user's sockets (the existing `session.end` close), so they reconnect and are checked again. A user never receives an event, diff or SQL for a database they cannot reach.

### Raw SQL and `query` cannot reach other databases (the main risk)

Both run with `search_path` set to one database, but a qualified name (`other.table`) still resolves. This must not become a way around grants. Requirements, with the recommended approach:

- The read-only `query` tool/endpoint and the intelligent-query path reject a statement whose plan touches any database the user cannot read: run `EXPLAIN (VERBOSE, FORMAT JSON)` first and check every relation's schema against `readable_schemas(user)` (plus `pg_catalog`/`information_schema` as today), then execute. View expansion shows underlying relations, so a view cannot hide them.
- Raw SQL proposals (`propose_sql`, scripts) must not name a database the proposer lacks editor on (qualified identifiers found by the existing SQL parser); the approver of the target database decides as today.
- **Decision (user): strong app-level check, no per-database Postgres roles.** In addition to the plan check, refuse statements that read catalog objects that list database names for a non-admin (`pg_namespace`, `pg_class`/`pg_tables` and other `pg_*` catalogs beyond what the app itself needs, `information_schema.schemata/tables/columns`), so names do not leak. **Known, accepted gap:** a function in a readable database that itself reads another database is not detected; document it in the README and the profile of the risk.
- If a robust check is not possible for a statement shape, refuse that shape with a plain message instead of allowing it.

### Management surfaces (admin only)

Web:

- `GET /api/users/<id>/access` → `{grants:[{schema, role}], cap}`; `PUT /api/users/<id>/access` with the full list (replaces atomically, one audit entry with before/after). `GET /api/users` includes each user's access summary for admins.
- Users page: per user an **Access** panel: an "All databases" row (none / viewer / editor / approver) and a row per database (inherit / none / viewer / editor / approver); levels above the user's global role are disabled with the reason; admin targets show "Admins reach every database"; explicit Save with the same confirm pattern as role changes for widening to approver.
- Add-user form gets the same picker (default none).

MCP (both admin-only tools; the same service):

- `get_user_access(email)` → grants + effective list of databases with levels.
- `set_user_access(email, grants:[{schema, role}])` → replaces the list; returns before/after. Direct apply (an admin's decision, same as the web); audited as `access.changed`.
- `list_users` (approver today) is unchanged and shows no grants; `get_user_access` is the way to see them.
- Parity test: both are COVERED entries (web `access` endpoints ↔ these tools), and `list_schemas` on MCP reflects the caller's grants.

Audit actions: `access.granted`, `access.revoked`, `access.changed` (actor, target email, before/after, via web or MCP).

### Profile

`/api/me` gains `access: {all: bool, schemas:[{name, role}]}` (admin: all databases at admin). Profile page adds **Your databases**: each reachable database with the level, and a line "Ask an admin for access to others".

### Edge cases to decide in code and test

- A grant for a database that no longer exists: ignored at read time, cleaned up when seen on write; never shown.
- User turned off: no access (already refused at sign-in/token).
- Global role lowered: effective access drops with it (cap), grants keep their stored values.
- `schema` absent still means `master`; if the user has no access to `master`, the request is refused like any unreachable database.
- Concurrent grant change while a request runs: effective role is read at request time; one request window is acceptable, sockets close on change.

## Review Focus

1. A user with no grant cannot see a database in any list, search, history, queue, audit, export, backup, event or error text, and cannot tell it exists.
2. `query`/intelligent query and raw SQL cannot read or write a database the user lacks, via qualified names, views, `schema.*`, `search_path` tricks or functions.
3. A grant above the global role never exceeds it; an editor cannot approve; a viewer-grant cannot propose; `*` + specific override behave as written.
4. Multi-database operations (`move_table`, `rename_schema`, `drop_schema`) require the right level on every database involved, for both proposing and deciding.
5. Only an admin can read or write grants, on web and MCP; a non-admin token never lists the access tools; self-grant impossible.
6. Existing users keep exactly their current access after migration; new users start with none.
7. Live events and sockets: no event for a database the user cannot reach; grant changes take effect on next event.
8. Profile and `/api/me` match what the server enforces for the same user.

## Constraints

- Backend Django 5 (`accounts`, `schema`, `gate`, `approvals`, `search`, `duplicates`, `scripts`, `iquery`, `chat`, `mcp_server`, `audit`), frontend Next.js 15 (`UsersView`, `ProfileView`, api/nav), Postgres + pgvector. Migrations must be safe and reversible. All checks go through `accounts/access.py`.
- Follow existing patterns: `@endpoint(min_role=...)` for global gates, `TOOL_ROLES` for MCP visibility, `tests/test_mcp_parity.py` COVERED/WEB_ONLY, audit via `record`.

## Left to the builder

Group naming for live events; the exact shape of the EXPLAIN check and its performance guard; how history/queue filters are expressed in each query; UI details of the Access panel within existing components and tokens.
