# Admin management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `admin` role, invite-only access (web sign-in and MCP), an admin Users page, role-checked approval over MCP, and a profile page showing what each role can do.

**Architecture:** `admin` becomes rank 3 above approver, so every existing `rank >= X` check keeps working and only user management and role-change approval check for admin. Sign-in stops auto-creating users: an email must already be an active user. User management is a small service (`accounts/services.py`) behind web-only `/api/users` endpoints. `/api/me` gains the MCP tool list for the caller's role; the web half of the profile reuses the frontend nav `minRole` table.

**Tech Stack:** Django 5 (`backend/accounts`, `approvals`, `mcp_server`), Next.js 15 (`frontend`), pytest, vitest.

**Spec:** `docs/superpowers/specs/2026-11-15-admin-management-design.md`

## Global Constraints

- Roles, in order: `viewer` < `editor` < `approver` < `admin` (ranks 0–3). `can_approve` stays `rank >= approver`; new `User.can_admin` is `rank >= admin`.
- Sign-in and MCP OAuth use one door: a verified email that belongs to an existing, **active** user. Unknown, off or unverified emails all get the same denial (`SignInDenied`, HTTP 403 `{"error": "not allowed"}`); nothing reveals which accounts exist.
- Emails are trimmed and lowercased; lookups are case-insensitive; one user per email.
- User management (list with off users, add, change role, turn on/off) and approving a change that contains a `set_user_role` op need `admin`. Every such action is audited (`user.created`, `user.role_changed`, `user.deactivated`, `user.reactivated`) with actor and target.
- Nobody changes their own role or turns themselves off. The last active admin can never be demoted or turned off.
- New `/api/users` routes are web-only in `tests/test_mcp_parity.py`; reason: "granting access is a person's decision; an AI client lists users with list_users". `list_users` (approver) stays on MCP.
- `auth.allowedDomains`, when non-empty, limits which emails an admin may add; empty means any email may be added.
- Commit trailers: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01QeZw812t2TxAkdSYBuXUz1`.

## Review Focus

1. A Gmail user who is not in the user list cannot sign in or finish MCP OAuth, and gets the same answer as an off user (Task 2 `test_unknown_email_denied_same_as_off_user`, `test_oauth_unknown_email_cannot_connect`).
2. The last active admin cannot be demoted, turned off or have their role changed, by themselves or another admin; two admins can manage each other (Task 3 `test_last_admin_is_protected`, `test_admins_manage_each_other`).
3. An approver who is not admin cannot add users, change roles, or approve a `set_user_role` config change, by web or MCP (Task 3 `test_approver_cannot_decide_role_change`, Task 4 `test_users_endpoints_need_admin`).
4. An editor or viewer MCP token calling `approve_change`, `reject_change` or `revert_change` is refused with the role message; a user turned off mid-session is refused on the next call; a role change applies on the next call (Task 5).
5. Email case and whitespace variants (`" Nolan@Gmail.com "`) map to one user and never create a duplicate (Task 3 `test_email_variants_are_one_user`).
6. The profile's MCP tools equal exactly what MCP `tools/list` returns for that role, and its web items equal the nav items shown (Task 4 `test_profile_tools_match_tools_list`, Task 6 `test_profile_lists_nav_items_for_role`).
7. A deploy or migration never locks out existing users: every existing user stays active with their role; with no active admin, the configured `seedAdmins` are promoted (Task 2 `test_seed_admin_bootstrap`).

---

### Task 1: The admin role

**Files:**
- Modify: `backend/accounts/models.py`, `backend/accounts/api.py`, `backend/mcp_server/tools.py` (`RANK`, line ~65)
- Create: `backend/accounts/migrations/0005_admin_role.py` (choices only)
- Test: `backend/tests/test_accounts_admin.py`

**Interfaces:**
- Produces: `Role.ADMIN = "admin"`; `ROLE_RANK[Role.ADMIN] == 3`; `User.can_admin`; `endpoint(..., min_role="admin")`; MCP `RANK["admin"] == 3` so an admin sees every tool an approver sees.

- [ ] **Step 1: Write failing tests:**
  - `test_admin_ranks_above_approver`: `can_approve` and `can_admin` true for admin; `can_admin` false for approver.
  - `test_endpoint_admin_min_role`: a tiny `@endpoint("GET", min_role="admin")` view gives 403 for approver, 200 for admin.
  - `test_admin_sees_approver_tools_over_mcp`: the tool list for an admin equals the list for an approver.
- [ ] **Step 2: Run** `pytest tests/test_accounts_admin.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the role, rank, `can_admin`, `ROLE_CHECK["admin"]`, MCP `RANK`, and the choices migration (`makemigrations accounts` with `DJANGO_SECRET_KEY` set).
- [ ] **Step 4: Run** the file, then `pytest tests/test_accounts.py tests/test_mcp_tools_registry.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(accounts): admin role`.

### Task 2: Invite-only sign-in and seed admins

**Files:**
- Modify: `backend/accounts/services.py` (`sign_in_from_claims`), `backend/accounts/apps.py` (post_migrate hook), `backend/config/siconfig.py` (comment `seedAdmins` semantics), `singularity.yaml` (comments)
- Test: `backend/tests/test_accounts_invite.py`; update `backend/tests/test_accounts.py`, `backend/tests/test_oauth.py` where they rely on auto-create

**Interfaces:**
- Consumes: Task 1.
- Produces: `sign_in_from_claims(claims, auth_cfg) -> User` — verified email required; looks up an existing active user by email (case-insensitive); otherwise `SignInDenied("not allowed")`. No user is created. First sign-in fills `first_name`/`last_name` from the claims when empty. `ensure_seed_admins(auth_cfg) -> list[User]` — creates each missing `seedAdmins` email as an active admin; if there is no active admin at all, promotes existing seed users to admin; otherwise leaves existing users alone. Runs on `post_migrate`.

- [ ] **Step 1: Write failing tests:**
  - `test_unknown_email_denied_same_as_off_user`: an unknown email and an inactive user raise `SignInDenied` with the same message, and no user row is created;
  - `test_existing_user_signs_in_case_insensitively`;
  - `test_unverified_email_denied`;
  - `test_oauth_unknown_email_cannot_connect`: the Google callback for an unknown email returns 403 and the OAuth authorize flow never reaches consent (use the existing oauth test helpers);
  - `test_seed_admin_bootstrap`: with no active admin, a configured seed email that is already an approver becomes admin and a missing one is created as admin; with an active admin present, an existing seed user is not changed; running twice changes nothing;
  - `test_existing_users_stay_active_after_bootstrap`.
- [ ] **Step 2: Run** `pytest tests/test_accounts_invite.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Drop the domain check and auto-create from `sign_in_from_claims` (the domain rule moves to invitations, Task 3). Hook `ensure_seed_admins` on `post_migrate` for the `accounts` app. Update the old tests that expected auto-create to create users first.
- [ ] **Step 4: Run** `pytest tests/test_accounts*.py tests/test_oauth.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(accounts): invite-only sign-in and seed admins`.

### Task 3: User management and role-change approval

**Files:**
- Modify: `backend/accounts/services.py`, `backend/approvals/service.py` (`can_decide`, `_dry_run_actor`), `backend/approvals/config_ops.py` (`_set_role`)
- Test: `backend/tests/test_accounts_manage.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces in `accounts/services.py`:
  - `normalize_email(raw: str) -> str` (trim + lowercase, must look like an email, ≤254 chars, else `UserManagementDenied`);
  - `create_user(actor, email, role, first_name="", last_name="") -> User` (admin only; role known; domain guard from `allowedDomains`; duplicate email refused);
  - `set_user_active(actor, target, active: bool) -> User`;
  - `change_role(actor, target, new_role) -> User` now needs `actor.can_admin`; refuses self-change; refuses demoting the last active admin (replaces the "last approver" rule); audited;
  - `class UserManagementDenied(Exception)` (and `RoleChangeDenied` becomes a subclass or alias so existing imports keep working).
- Produces: `approvals.service.can_decide(user, change)` returns `user.can_admin` for a config change whose operations include `set_user_role`, else unchanged; `_dry_run_actor` picks an active admin for role ops and an active approver-or-admin otherwise.

- [ ] **Step 1: Write failing tests:**
  - `test_admin_creates_user_with_role` (audit row `user.created`); `test_non_admin_cannot_create_or_change` (editor, approver);
  - `test_email_variants_are_one_user`: `" Nolan@Gmail.com "` then `nolan@gmail.com` → second refused as duplicate;
  - `test_domain_guard`: with `allowedDomains=["example.com"]` an `x@other.com` invite is refused; with an empty list it is allowed;
  - `test_cannot_change_own_role_or_turn_self_off`;
  - `test_last_admin_is_protected`: demote or turn off the only active admin → refused, even by the admin themself via another path; `test_admins_manage_each_other`: with two admins, one can demote the other;
  - `test_turned_off_user_cannot_sign_in` (links to Task 2 behaviour) and is audited `user.deactivated`; reactivate audited;
  - `test_approver_cannot_decide_role_change`: an editor proposes a `set_user_role` config change; an approver's `approve` raises `ChangeError("forbidden")`; an admin's succeeds and the role changes; a non-role config change is still approvable by an approver.
- [ ] **Step 2: Run** `pytest tests/test_accounts_manage.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the service functions and the two `approvals` changes; `_set_role` keeps calling `change_role`.
- [ ] **Step 4: Run** the file, then `pytest tests/test_accounts*.py tests/test_approvals*.py tests/test_changes*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(accounts): admin user management and admin-only role approval`.

### Task 4: Users endpoints and the profile payload

**Files:**
- Modify: `backend/accounts/views.py`, `backend/accounts/urls.py`, `backend/tests/test_mcp_parity.py`
- Create: `backend/accounts/capabilities.py`
- Test: `backend/tests/test_users_api.py`

**Interfaces:**
- Consumes: Tasks 1–3, `mcp_server.tools.TOOL_ROLES` and the MCP tool descriptions.
- Produces (all `@endpoint(..., min_role="admin")`):
  - `GET /api/users` → `{users: [{id, email, name, role, active, lastLoginAt}]}` (includes off users);
  - `POST /api/users` `{email, role, name?}` → user JSON, 201;
  - `PATCH /api/users/<id>` `{role?, active?}` → user JSON. Errors: `UserManagementDenied` → 400, unknown id → 404.
  - The old `PATCH /api/users/<id>/role` stays and calls the same service (so the existing Settings page keeps working until Task 6).
- Produces: `GET /api/me` returns today's fields plus `{active: true, capabilities: {mcpTools: [{name, minRole, allowed, description}], roleSummary: [str]}}`. `capabilities.build(user)` lists every tool in `TOOL_ROLES` with `allowed = rank >= tool's rank`, and `roleSummary` is a short fixed text per role ("Read data and the schema", "Edit data, import files, propose schema changes", "Approve changes, settings, backups", "Manage users and roles").

- [ ] **Step 1: Write failing tests:**
  - `test_users_endpoints_need_admin`: viewer/editor/approver → 403 on GET, POST, PATCH; anonymous → 401;
  - `test_admin_lists_creates_patches_users` including an off user in the list and `lastLoginAt`;
  - `test_users_validation`: bad email, unknown role, duplicate → 400; unknown id → 404; self role change → 400;
  - `test_profile_tools_match_tools_list`: for each role, `me.capabilities.mcpTools` entries with `allowed` equal the tool names MCP `tools/list` returns for a token of that role (use the in-process MCP helpers from `tests/test_mcp_http.py`);
  - `test_me_has_role_summary`;
  - parity: the three new routes appear in `WEB_ONLY` with the constraint's reason (the parity test must pass).
- [ ] **Step 2: Run** `pytest tests/test_users_api.py tests/test_mcp_parity.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** views, urls, `capabilities.py`, `_user_json` (adds `active`, `lastLoginAt` from `last_login`; make sign-in set `last_login`).
- [ ] **Step 4: Run** them, then `pytest tests/test_accounts*.py tests/test_oauth.py tests/test_mcp_*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(accounts): users endpoints and profile capabilities`.

### Task 5: Approving over MCP checks the real role

**Files:**
- Modify: `backend/mcp_server/tools.py` (descriptions only if needed: admin rows, role-change approval note), `backend/mcp_server/app.py` (instructions text)
- Test: `backend/tests/test_mcp_roles.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: tests only, plus text changes: the server instructions say a role change needs an admin to approve, and `approve_change` returns the existing refusal for a role-change config change when the token's user is not admin.

- [ ] **Step 1: Write failing tests** (use a real token per role via `accounts.tokens.create_token`, call tools through the in-process MCP app as `tests/test_mcp_http.py` does):
  - `test_editor_and_viewer_tokens_cannot_approve_reject_revert`: each of the three tools returns the role error and the change stays pending;
  - `test_approver_token_approves_data_change`; `test_approver_token_cannot_approve_role_change`; `test_admin_token_approves_role_change`;
  - `test_turned_off_user_token_refused_next_call`: create token, call OK, turn the user off, next call returns 401;
  - `test_role_change_applies_next_call`: editor token can call an editor tool, admin demotes to viewer, the same token is then refused;
  - `test_unknown_email_cannot_get_a_token`: there is no user for the email, so `create_token` is unreachable through sign-in (covered by Task 2) — assert `tokens.authenticate` returns `None` for a token of a deleted user.
- [ ] **Step 2: Run** `pytest tests/test_mcp_roles.py -q`. Expected: FAIL only where text/behaviour is missing; if a test passes at once, keep it as a regression pin and note it in the report.
- [ ] **Step 3: Implement** whatever the failing tests need (expected: refusal text for admin-only role approval in `approve_change`, instructions text).
- [ ] **Step 4: Run** the file, then `pytest tests/test_mcp_*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `test(mcp): approval checks the caller's real role`.

### Task 6: Users page, Profile page, nav

**Files:**
- Create: `frontend/components/UsersView.tsx` (+ test), `frontend/components/ProfileView.tsx` (+ test)
- Modify: `frontend/lib/api.ts` (`Role` gains `"admin"`, `Me` gains `active`, `lastLoginAt?`, `capabilities?`; functions `listUsers`, `createUser`, `updateUser`), `frontend/lib/nav.ts` (`RANK.admin = 3`; item `{ view: "users", label: "Users", icon: "users" or nearest existing, minRole: "admin" }` in System; profile reachable from the user menu, not the nav), `frontend/lib/route.ts` (`users`, `profile` views and paths `/users`, `/profile`), `frontend/components/AppShell.tsx` (render both views, user-menu link to Profile, admin guard → `NotFound` for others), `frontend/components/SettingsView.tsx` (team section links to Users for admins and keeps a read-only list otherwise), route and nav tests
- Test: `frontend/components/UsersView.test.tsx`, `ProfileView.test.tsx`, `lib/route.test.ts`, `lib/nav.test.ts`

**Interfaces:**
- Consumes: Task 4 payloads.
- Produces: `UsersView` (table: email, name, role select, active toggle, last sign-in; "Add user" form: email + role; own row and the last admin show disabled controls with a reason; inline errors), `ProfileView` (name, email, role badge, role summary, "You can" = nav items the role may open (from `nav.ts`) + allowed MCP tools, "Needs a higher role" = the rest with the minimum role, link to Connect).

- [ ] **Step 1: Write failing tests (mock the API):**
  - `test_users_view_lists_adds_and_changes`: lists, add user sends `{email, role}`, role select calls `updateUser(id, {role})`, active toggle calls `updateUser(id, {active})`;
  - `test_users_view_disables_self_and_last_admin` with a reason text;
  - `test_users_view_shows_server_errors`;
  - `test_profile_lists_nav_items_for_role`: for viewer, editor, approver, admin the "You can" list equals `navFor(role)` items and the MCP tools equal the `allowed` entries; "Needs a higher role" names the minimum role;
  - route round-trips `/users` and `/profile`; the nav shows Users only for admin; a non-admin hitting `/users` sees NotFound.
- [ ] **Step 2: Run** `npx vitest run components/UsersView.test.tsx components/ProfileView.test.tsx lib/route.test.ts lib/nav.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement**, using existing UI parts (`Button`, `Badge`, `Panel`) and tokens, like `SettingsView`.
- [ ] **Step 4: Run** those, then the full `npx vitest run`, `npm run typecheck`, `npm run build`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(accounts): users and profile pages`.

### Task 7: Docs and full check

**Files:** Modify `README.md` (a short "Access" section: roles incl. admin, invite-only, how to add users, seedAdmins and bootstrap, MCP approval, profile), `singularity.yaml` (comments for `seedAdmins` and `allowedDomains`).

- [ ] **Step 1:** Backend `pytest -q`. Expected: all pass.
- [ ] **Step 2:** Frontend `npx vitest run`, `npm run typecheck`, `npm run build`. Expected: all pass.
- [ ] **Step 3: Commit** `docs(accounts): access, roles and the admin page`.
