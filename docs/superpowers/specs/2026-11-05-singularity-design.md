# Singularity — Design Spec

**Date:** 2026-11-05
**Status:** Draft, awaiting review
**Type:** Architectural (new project)

---

## 0. Summary

Singularity is an **internal team tool** for designing and managing a **master data** database in **PostgreSQL** through natural-language prompts. AI clients (a built-in web chat via OpenRouter, plus external MCP clients such as Claude Desktop and Cursor) act on the database **only through an MCP server**, and every change passes a configurable **rules gate** and an **approval flow**. It also includes a RAG search box ("where does this kind of data belong?"), a Rust engine (WASM in the browser, PyO3 in Django) for heavy computation, and a 3D "schema universe" UI.

### Decisions made with the user
| Topic | Decision |
|---|---|
| Audience | Internal tool for the user's team |
| Deployment | Docker Compose; **nginx is the only exposed entry point** (reverse proxy) |
| Database | One main Postgres (the team's stack is Postgres) |
| Focus | **Master data** first |
| AI clients | **Both** a web chat (OpenRouter) and external MCP clients |
| Auth | Google OAuth everywhere (web and MCP) |
| Permissions | Roles: read / read+edit / read+edit+approve, config- and settings-driven |
| Approvals | Data edits: self-approve (editor+). Schema changes: approver only; editor schema proposals go to a review queue |
| Approval UI | Web popup, or confirmation inside the connected AI tool |
| Rules storage | In the database, edited in the web Settings UI by approvers |
| MCP placement | Option 1: one Django codebase, separate `mcp` container |
| Rust engine | 3D layout (WASM), in-browser stats (WASM), duplicate detection (PyO3) |
| UI | 3D universe with a singularity theme, plus a 2D toggle |
| Embeddings | Local **multilingual-e5-small** on CPU (no GPU); names in English, values may be Thai |
| v1 scope | Everything, built in 7 milestones (section 8) |
| User roles | Managed in the Settings UI; first admins seeded from YAML |

### Assumptions (not explicitly stated by the user)
- The team uses a single Google Workspace domain.
- The default naming convention is **camelCase**, configurable per rule (snake_case also supported).
- Scale: up to a few hundred tables and up to millions of rows per master table.
- Celery + Redis for background jobs.

---

## 1. System architecture

### Containers (`docker-compose.yml`)
| Container | Role | Exposed |
|---|---|---|
| `nginx` | Gateway, TLS, routing | **Yes (443, 80→443 redirect)** |
| `web` | Next.js: UI, 3D, WASM | No |
| `api` | Django REST API, approvals, chat orchestration, WebSocket (Channels) | No |
| `mcp` | Same Django codebase, runs MCP server (Python MCP SDK, streamable HTTP) | No |
| `worker` | Celery: re-embedding, duplicate scans, backups | No |
| `embed` | multilingual-e5-small on CPU (Text Embeddings Inference CPU image or FastEmbed/ONNX) | No |
| `db` | PostgreSQL + pgvector | No |
| `redis` | Celery broker, Channels layer | No |

### nginx routes
| Path | Upstream |
|---|---|
| `/` | `web` |
| `/api/` | `api` |
| `/ws/` | `api` (WebSocket upgrade) |
| `/mcp` | `mcp` |

### Repo layout
```
singularity/
├─ frontend/            Next.js (App Router, TypeScript)
├─ backend/             Django project (api + mcp + worker entrypoints)
│   ├─ accounts/          Google OAuth, roles
│   ├─ gate/              rules engine, operation validation, SQL generation
│   ├─ approvals/         change lifecycle, queue, diffs, revert
│   ├─ schema/            introspection, apply in transaction
│   ├─ search/            embeddings, hybrid search
│   ├─ quality/           duplicate groups (calls Rust via PyO3)
│   ├─ chat/              OpenRouter client, conversation history
│   ├─ settings_app/      DB-stored settings and rules (versioned)
│   ├─ audit/             audit log
│   └─ mcp_server/        MCP tool definitions (thin layer over the apps above)
├─ engine/              Rust crate → WASM (wasm-bindgen) + Python module (PyO3/maturin)
├─ nginx/
├─ singularity.yaml     deploy-time config
├─ .env.example
└─ docker-compose.yml
```

### Database layout (one Postgres)
- Schema `singularity`: internal tables (users, roles, rules, rule_versions, settings, changes, audit_log, schema_embeddings, duplicate_groups, conversations).
- Schema `master`: the team's master data, designed and edited by the AI.
- DB roles:
  - `singularity_app`: full access to `singularity`, used by Django for internal state.
  - `singularity_master_rw`: DDL and DML on `master` only. Used **only** by the gate's apply step.
  - `singularity_master_ro`: SELECT on `master` only. Used for `query`, introspection and stats.

**Core principle:** the rules gate (in `backend/gate`) is the only code path that writes to `master`. API, web chat, grid editor, and MCP all call it.

---

## 2. Rules gate and approval flow

### Structured operations (no raw write SQL)
The AI submits a list of operations; the gate validates them and generates the SQL.

Schema ops: `create_table`, `add_column`, `rename_table`, `rename_column`, `alter_column_type`, `set_column_comment`, `add_index`, `add_foreign_key`, `add_unique`, `drop_column`, `drop_table`, `drop_index`, `drop_constraint`.
Data ops: `insert_rows`, `update_rows` (by primary key or filter with a max-row limit), `deactivate_rows` (soft delete).

Raw SQL is accepted **only** as read-only `SELECT`, run as `singularity_master_ro` with `statement_timeout` and a row limit.

Example:
```json
{ "op": "create_table", "name": "supplierContact",
  "columns": [{ "name": "phoneNumber", "type": "text", "comment": "Main contact phone" }] }
```

Generated SQL always quotes identifiers. Default columns from rules (for example `id`, `createdAt`, `updatedAt`, `createdBy`, `isActive`) are added automatically if missing, and the diff shows this.

### Rule types
| Type | Examples |
|---|---|
| Naming | Table and column case (camelCase or snake_case), reserved words, max length, singular/plural |
| Required | Columns every table must have; every table needs a business key (unique) |
| Forbidden | Hard delete, `drop_*` without a reason, free-text columns named `*Code` without a reference FK |
| Types | Mappings such as money → `numeric(18,2)`; disallow `float` for money; timestamps use `timestamptz` |
| Relationships | FKs must reference a primary key; FK column naming (for example `<table>Id`) |

Each rule has: `id`, `type`, `params` (JSON), `severity` (`block` or `warn`), `enabled`, `description`.
Rules are stored in `singularity.rules`; every edit writes a row to `singularity.rule_versions` (who, when, before, after). Approvers can roll back to an earlier version.

### Change lifecycle
```
proposed → checked → pending → approved → applied
              ↓          ↓                  ↓
          rejected   rejected        failed (rolled back)
                         ↓
                       stale (schema changed since check; must re-check)
```
1. **Proposed:** operations submitted with requester, source (web chat, MCP client, grid editor), prompt and conversation link.
2. **Checked:** gate validates. `block` violations reject with reasons and fix suggestions; `warn` violations are attached.
3. **Pending:** diff and generated SQL stored. Routed by the approval table below.
4. **Approved → Applied:** pre-change backup (schema changes only), then all SQL runs in **one transaction** as `singularity_master_rw`. On error the change rolls back and is marked `failed` with the Postgres error.
5. **Stale:** each proposal records a schema fingerprint (hash of affected tables). If it differs at approval time, the change becomes `stale` and must be re-checked.

### Approval table
| Change | Viewer | Editor | Approver |
|---|---|---|---|
| Read / query | ✅ | ✅ | ✅ |
| Data edit | ❌ | Self-approve | Self-approve |
| Schema change | ❌ | Goes to queue; any approver decides | Self-approve |
| Review queue | ❌ | ❌ | ✅ |
| Edit rules and settings | ❌ | ❌ | ✅ |

Self-approval must come from the **same signed-in user** who proposed (checked server-side).

### Audit and revert
- `singularity.audit_log` records every proposal, decision, apply, failure, rule change and settings change: who, when, source, prompt, operations, SQL, result.
- Each applied change stores its **reverse operations** where possible (for example `add_column` → `drop_column`). Approvers can revert from History; a revert is itself a new change through the gate. Irreversible operations (`drop_table`, `drop_column` with data) are marked as such, and revert restores from the pre-change backup only by manual admin action.

---

## 3. MCP tools and web chat

### MCP tools
| Tool | Min role | Purpose |
|---|---|---|
| `get_rules` | viewer | Active rules, so the AI follows them up front |
| `list_tables` | viewer | Tables in `master` with row counts and comments |
| `describe_table` | viewer | Columns, types, constraints, relationships, sample values |
| `search_schema` | viewer | RAG search: "where does X belong?" |
| `query` | viewer | Read-only SELECT with limits |
| `find_duplicates` | viewer | Duplicate groups for a table (existing or a new scan) |
| `propose_change` | editor | Submit operations; returns check result, diff and change ID |
| `get_change` | editor | Status and details of a change |
| `approve_change` | editor | Self-approve per the approval table |
| `reject_change` | editor | Withdraw your own proposal (approvers: reject any pending) |
| `list_pending` | approver | Review queue |

Tools above the user's role are **not listed** in that user's MCP session.

### MCP transport and auth
- Streamable HTTP at `https://<host>/mcp`.
- OAuth 2.1 per the MCP authorization spec, with Google as the identity provider; Singularity issues its own session tokens mapped to the user and role.
- Approval in external clients: after `propose_change`, the tool result includes the diff and instructs the client to ask the user; `approve_change` is called on confirmation. The web popup for the same change appears simultaneously, and whichever comes first wins.

### Web chat
- Django calls **OpenRouter** (chat completions with tool calling) and executes the same MCP tool functions in-process for the signed-in user, so the behavior matches external clients.
- Model: a default plus an allowed list (YAML); the default can be changed in Settings. API key server-side only.
- Approval popup over WebSocket: diff (added green, removed red), generated SQL (collapsible), warnings, Approve or Reject.
- Conversations are stored per user; each change links to its conversation.

### Limits (YAML)
`maxQueryRows` (default 500), `queryTimeoutMs` (default 5000), `maxOpsPerProposal` (default 50), `maxRowsPerDataOp` (default 1000), per-user chat rate limit.

---

## 4. RAG search and Rust engine

### Embedded documents
One document per table and per column, built from name, type, comment, relationships and 3–5 sample values:
```
passage: table customer, column phoneNumber (text). Description: main contact phone. Examples: 081-234-5678, 02-123-4567
```
Stored in `singularity.schema_embeddings` (`vector(384)`, HNSW index). Queries are embedded with the `query: ` prefix (required by E5).

### Freshness
- After an applied schema change: worker re-embeds affected tables.
- Sample values: refreshed nightly (schedule in Settings).
- "Rebuild index" button (approvers); required after changing the embedding model.
- Model name and vector size come from YAML.

### Search
- Hybrid: vector similarity combined with keyword/trigram match on names (`pg_trgm`), so exact names rank first.
- Returns top 5 with confidence. If the best score is below a threshold, it offers "Ask AI to propose a new column", opening the chat pre-filled.
- Works with Thai or English queries.

### Rust engine (`engine/`)
| Module | Target | Use |
|---|---|---|
| `layout` | WASM | 3D force-directed layout for the universe view |
| `stats` | WASM | Column stats in the browser on fetched samples: counts, distributions, null % |
| `dedup` | PyO3 (worker) | Duplicate detection on master tables |

**Dedup pipeline:** normalize (case, whitespace, punctuation, company suffixes such as "Co., Ltd." / "Company Limited" / "บริษัท … จำกัด", Thai whitespace and zero-width characters) → blocking (to avoid N² comparisons) → similarity scoring (for example Jaro-Winkler plus token-based) → groups above a threshold saved to `singularity.duplicate_groups`. Merging is a normal data change through the gate.

**Fallbacks:** if WASM fails to load, the UI uses a simple JS 2D layout and server-side stats.

---

## 5. Web UI

### Pages
| Page | Min role | Content |
|---|---|---|
| Universe (home) | viewer | 3D schema map with search on top |
| Chat | viewer | AI chat with approval popups |
| Tables | viewer | 2D list and ER view; grid editor per table (editor+ can edit) |
| Queue | approver | Pending schema changes: diff, prompt, requester |
| History | viewer | Audit log; revert (approver) |
| Duplicates | editor | Duplicate groups to review and merge |
| Settings | approver | Rules, users and roles, default model, toggles, index rebuild |

### 3D Universe (React Three Fiber)
- Core master entities form the glowing central **singularity**; tables are stars (size = row count, color = domain); FKs are glowing gravity lines; reference tables orbit their users.
- Click a star: side panel with columns, stats and "ask AI about this table".
- Search: camera flies to the match and highlights the field.
- Live: pending change shows as a faint protostar; it ignites on apply; tables with open duplicate groups pulse red.
- Quality toggle (low/high) with automatic degradation on weak devices; 2D toggle always available.

### Grid editor
- Spreadsheet-style editing with filter, sort and server-side paging.
- Edits collect as a draft; Save creates one `update_rows`/`insert_rows` proposal through the gate; self-approve popup.
- FK columns to reference tables render as dropdowns.

### Look and feel
Dark space theme (deep navy/black, glowing accents). UI in Thai and English (i18n).

### Live updates
WebSocket (Django Channels) for approval popups, queue counts, change status and star ignition.

---

## 6. Config, error handling, testing

### Config layers
1. **Deploy-time** (`.env` + `singularity.yaml`, restart required)
   - `.env`: Google OAuth client ID/secret, OpenRouter key, DB passwords, Django secret.
   - YAML: allowed Google domain(s), seed admin emails, default role for new users, embedding model and dimension, limits, allowed LLM models, backup retention.
2. **Runtime** (DB, Settings UI, approvers only, versioned and audited)
   - Rules, user roles, default chat model, feature toggles (3D, dedup schedule), sample-refresh schedule.

### Error handling
| Situation | Behavior |
|---|---|
| Gate rejects | Reasons plus fix suggestions returned to AI and user, so the AI can retry |
| Apply fails | Full rollback; change marked `failed` with Postgres error |
| Schema drift | Proposal marked `stale`; must re-check |
| OpenRouter down | Chat shows error; rest of the app works |
| Embed service down | Search falls back to keyword-only |
| WASM fails | 2D JS layout, server stats |
| Unauthorized tool call | Rejected and audited |

### Backups
- Automatic schema-and-affected-table `pg_dump` before every applied schema change.
- Nightly full backup; retention from YAML; stored in a Docker volume.

### Testing
- **Gate:** unit tests for every rule type and operation, including SQL generation and quoting. Highest priority.
- **Approvals:** tests for every role × change-type cell in the approval table, plus same-user self-approval and stale detection.
- **MCP:** integration tests acting as a real MCP client against a test Postgres, including role-based tool visibility.
- **Search:** fixture schema with English names and Thai values; expected top results for Thai and English queries.
- **Rust:** `cargo test` for dedup (including Thai normalization), layout and stats; Python binding smoke test.
- **Frontend:** component tests; Playwright end-to-end: sign in → propose via chat → approve → change applied → star ignites.
- **CI:** all tests run in Docker Compose, same images as production.

---

## 7. Security notes
- Only nginx is exposed; all services on an internal Docker network.
- The AI never gets DB credentials; it only calls tools.
- Write access to `master` exists only in the gate's apply step (`singularity_master_rw`).
- Read queries run as `singularity_master_ro` with timeouts and row limits.
- Secrets only in `.env` (not committed).
- Every action is attributed to a Google-authenticated user and audited.

---

## 8. Milestones (all in v1, built in order)
1. **Foundation:** Docker Compose, nginx, Postgres + pgvector, Django + Next.js skeletons, Google OAuth, roles (YAML seed + Settings).
2. **Core:** rules engine, structured operations, SQL generation, change lifecycle, approvals and queue, audit log, backups, revert, MCP server with tools.
3. **Web chat:** OpenRouter integration, approval popups over WebSocket, 2D schema view.
4. **Search:** embed container, indexing jobs, hybrid search, search box.
5. **Data:** grid editor through the gate, reference dropdowns.
6. **Rust engine:** crate with `layout`, `stats` (WASM) and `dedup` (PyO3); Duplicates page.
7. **3D Universe:** React Three Fiber scene, live effects, quality toggle.

Each milestone is usable on its own and gets its own implementation plan.

---

## 9. Out of scope for v1
- Multiple target databases (one main Postgres only).
- Non-Google sign-in.
- Public or multi-tenant SaaS use.
- Fine-tuning the embedding model.
