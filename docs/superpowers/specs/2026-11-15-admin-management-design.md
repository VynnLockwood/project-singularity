# Admin management, invite-only access, profile page — design

## Intent

Today anyone whose email domain is in `auth.allowedDomains` can sign in with Google and becomes a user, and a user can then mint an MCP token. With `gmail.com` allowed, that is every Gmail account. We want access controlled by people: only people an admin has added can sign in or connect an AI client, and an admin can manage who they are and what they may do. Each person can also see, on a profile page, exactly what their role lets them do.

## Goals

- A new top role, **admin**, above approver: roles are viewer < editor < approver < admin.
- `nolanrobinson490@gmail.com` is the first admin (seed + migration of the existing account).
- **Invite-only access.** A Google sign-in whose email is not an existing, active user is rejected. The MCP OAuth flow and token creation go through the same sign-in, so an unknown email cannot connect an AI client either. Every MCP request still resolves a token to a user, so we always know the email behind an MCP call.
- Admin page (web): list users, add a user (email, role, optional name), change a role, turn a user off or on. Every action is audited.
- Approving through MCP: `approve_change`, `reject_change`, `revert_change` already exist and are approver-only; keep them and prove with tests that the caller's real role from their token is what is checked (editor and viewer tokens are refused, an off user is refused, self-approval rules still hold).
- Profile page: who I am (name, email, role), what I can do in the web app and over MCP, and what needs a higher role. The list is generated from the same role tables the code enforces (nav `minRole`, `TOOL_ROLES`, endpoint `min_role`), so it cannot drift.

## Non-goals

- Custom roles or a per-permission matrix (option B was considered and rejected: it touches every check in web and MCP; three-plus-one fixed roles cover the need).
- Managing users over MCP. An AI client must not grant access (same reason as tokens). `list_users` stays; `propose_config_change` `set_user_role` keeps working but now only an admin can approve it.
- Changing how Google sign-in works, SSO other than Google, passwords.
- Per-database permissions.

## Anti-goals

- Existing signed-in users must not be locked out by the migration (every existing user stays active with their current role; only the seed admin is raised to admin).
- No way for a non-admin to give themselves or anyone else access or a higher role, by web or MCP.
- A rejected email must not learn whether an account exists (same generic "not allowed" answer for unknown, off, or unverified emails).

## Approaches considered

1. **Admin as a fourth role (chosen).** Every existing check is "rank ≥ X", so an admin passes all approver checks with no change; only user management checks for admin. One field, one place to check.
2. Admin as a separate `is_admin` flag. More flexible but two fields to check everywhere; easy to miss one; worse for security.
3. Custom roles with a permission matrix. Most flexible; large blast radius. Rejected for now.

## Design

### Roles and users

- `Role` gains `ADMIN = "admin"`, `ROLE_RANK` gets admin = 3; `User.can_admin` property (rank ≥ admin). `can_approve` stays rank ≥ approver (so admins can approve). Migration lifts `nolanrobinson490@gmail.com` to admin if the user exists; for a fresh install `seedAdmins` (singularity.yaml and `siconfig` default) are pre-created as active admin users at startup/migration instead of "becoming approver on first sign-in".
- "Last approver" safeguard becomes "last active admin": the last admin cannot be demoted or turned off.

### Invite-only sign-in

- `sign_in_from_claims`: require a verified email, then look up an existing active user by email (case-insensitive). No user, or user off → `SignInDenied` (generic message). No auto-create. `allowedDomains` becomes an optional guard on invitations: when non-empty, an admin may only add emails in those domains; when empty, any email can be invited.
- `defaultRole` setting is dropped from sign-in (new users get the role the admin picks); keep it parsed but unused only if removing breaks config loading — decide in the plan.
- MCP: OAuth authorization already requires web sign-in and tokens are bound to a user; `tokens.authenticate` already rejects off users. Add tests: an unknown email cannot complete OAuth; a user turned off loses MCP access on the next request; a role change applies on the next request.

### Admin endpoints (web only)

- `GET /api/users` (admin: full list incl. off users), `POST /api/users` (add: email, role, first/last name optional), `PATCH /api/users/<id>` (role and/or active). All `min_role="admin"`, WEB_ONLY parity reason "granting access is a person's decision; an AI client lists users with list_users".
- Rules: cannot change your own role or turn yourself off; cannot remove the last active admin; email validated and lowercased; unique; domain guard as above; role must be a known role; every action audited (`user.created`, `user.role_changed`, `user.deactivated`, `user.reactivated`) with actor and target.
- Existing `PUT /api/users/<id>/role` is folded into this (kept as an alias if the frontend/MCP parity needs it) and now requires admin instead of approver.
- `propose_config_change` `set_user_role`: the proposal can be made by an editor/approver (as today), but approving a change that contains a role change or user management requires admin; an approver sees it but cannot decide it.

### Profile (`GET /api/me` extended, `profile` view)

- `/api/me` returns, in addition to today's fields, `capabilities`: for each nav view and each MCP tool the user's role allows or does not (with the minimum role needed), generated from `nav.ts` `minRole`, `mcp_server.tools.TOOL_ROLES` and endpoint `min_role`. A backend `accounts.capabilities` module builds the web part from a single server-side table; the frontend nav keeps its own `minRole` and a test asserts both agree.
- Page `/profile`: name, email, role badge, "You can" (grouped: read, edit data, import, approve, administer), "You cannot yet — needs <role>" list, link to Connect (tokens). Reachable from the user menu for everyone.

### Admin page

- Nav item "Users" (admin only, `minRole: "admin"`, Admin group) and route `/users`: table (email, name, role select, active toggle, last sign-in), "Add user" form (email, role), inline errors, the last-admin and self rules shown as disabled controls with a reason. The existing user list in Settings is replaced by a link to this page.

## Security considerations

- Authorization is enforced server-side on every endpoint and MCP tool; the UI only hides.
- Unknown / off / unverified emails get one generic denial; nothing reveals which accounts exist.
- CSRF and `@endpoint` patterns as for other POST/PATCH routes.
- Audit trail for every user management action; no secrets in it.
- Migration is idempotent and reversible (role choices change only; no data loss).

## Review focus (inputs the plan's tests must pin)

1. An existing user with a Gmail address who is NOT in the user list cannot sign in or finish MCP OAuth, and gets the same message as an off user.
2. Last active admin cannot be demoted, turned off, or have their role changed by themselves; two admins can manage each other.
3. An approver (not admin) cannot add users, change roles, or approve a `set_user_role` config change, by web or MCP.
4. An editor or viewer MCP token calling `approve_change`/`reject_change`/`revert_change` is refused with the role message; a user turned off mid-session is refused on the next call.
5. Email case/whitespace variants (`Nolan@Gmail.com `) map to the same user and cannot create duplicates.
6. The profile lists exactly the tools the user's MCP `tools/list` returns, and exactly the nav items the web shows (test asserts equality per role).

## Left to the builder

Exact field names for the capability payload, UI layout, whether to keep `defaultRole` in config, migration file names, and how the seed admin is created on a fresh install (data migration vs startup hook).
