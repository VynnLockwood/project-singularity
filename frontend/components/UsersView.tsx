"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, ROLES, createUser, listUsers, updateUser, type Me, type Role, type User } from "@/lib/api";
import { Button, Panel, Skeleton } from "@/components/ui";

/** A light check only: the server validates the address and the allowed domains. */
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Server messages come in lower case ("cannot remove the last active admin"); show them as sentences. */
function sentence(e: unknown, fallback: string): string {
  const m = (e instanceof ApiError ? e.message : fallback).trim();
  if (!m) return "";
  const s = m.charAt(0).toUpperCase() + m.slice(1);
  return /[.!?]$/.test(s) ? s : `${s}.`;
}

/** A refusal plus what to do about it, where the server's message alone leaves you stuck. */
function refusal(e: unknown): string {
  const m = sentence(e, "Could not save this change. Try again.");
  return /last active admin/i.test(m) ? `${m} Make someone else an admin first.` : m;
}

const lastSignIn = (at: string | null) => (at ? new Date(at).toLocaleString() : "Never");

function AddUser({ onAdded }: { onAdded: () => void }) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(""); setDone("");
    const address = email.trim();
    if (!EMAIL_RE.test(address)) { setError("Enter an email address like name@example.com."); return; }
    setBusy(true);
    try {
      const who = name.trim();
      const added = await createUser({ email: address, role, ...(who ? { name: who } : {}) });
      setDone(`${added.email} can now sign in as ${added.role}.`);
      setEmail(""); setName(""); setRole("viewer");
      onAdded();
    } catch (x) {
      setError(sentence(x, "Could not add this person. Try again."));
    } finally { setBusy(false); }
  };

  return (
    <Panel title="Add user">
      <form className="users-add" aria-label="Add user" onSubmit={submit} noValidate>
        <div className="users-add-fields">
          <label>Email
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@example.com"
              autoComplete="off" aria-invalid={error ? "true" : undefined} aria-describedby={error ? "add-user-error" : undefined} required />
          </label>
          <label>Name <span className="muted small">(optional)</span>
            <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
          </label>
          <label>Role
            <select id="add-user-role" value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <Button type="submit" variant="primary" icon="plus" disabled={busy}>Add user</Button>
        </div>
        <p className="muted small">They sign in with Google using this address. Nobody else can sign in or connect an AI client.</p>
        {error && <p id="add-user-error" className="problems" role="alert">{error}</p>}
        {done && <p className="muted small" role="status">{done}</p>}
      </form>
    </Panel>
  );
}

/** Admins add people, set their role and turn their access on or off. The server enforces every rule; this page shows them. */
export function UsersView({ me }: { me: Me }) {
  const [users, setUsers] = useState<User[] | null>(null);
  const [loadError, setLoadError] = useState("");
  const [rowError, setRowError] = useState<{ id: number; message: string } | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  /** Roles picked but not saved yet, by user id: a stray arrow key in a select must not change anyone's access. */
  const [staged, setStaged] = useState<Record<number, Role>>({});
  const [confirming, setConfirming] = useState<number | null>(null);

  const load = useCallback(() => listUsers().then((u) => { setUsers(u); setLoadError(""); })
    .catch((e) => setLoadError(["Could not load people.", sentence(e, ""), "Reload the page to try again."].filter(Boolean).join(" "))), []);
  useEffect(() => { load(); }, [load]);

  const focusRole = (id: number) => document.getElementById(`user-${id}-role`)?.focus();

  const change = async (u: User, fields: { role?: Role; active?: boolean }) => {
    setRowError(null); setConfirming(null); setBusy(u.id);
    try {
      await updateUser(u.id, fields);
      if (fields.role) setStaged((s) => { const n = { ...s }; delete n[u.id]; return n; });
    } catch (e) {
      setRowError({ id: u.id, message: refusal(e) });
    }
    await load();
    setBusy(null);
  };

  const save = (u: User) => {
    const role = staged[u.id];
    if (!role || role === u.role) return;
    if (role === "admin") { setConfirming(u.id); return; } // granting admin is asked about first
    change(u, { role });
  };

  const activeAdmins = (users ?? []).filter((u) => u.role === "admin" && u.active).length;
  const reason = (u: User): string => {
    if (u.id === me.id) return "This is you. Another admin can change your role or access.";
    if (u.role === "admin" && u.active && activeAdmins <= 1) return "The last active admin. Make someone else an admin first.";
    return "";
  };

  return (
    <div className="users">
      <h2>Users</h2>
      <p className="muted">
        Only the people listed here can sign in. Their role decides what they can do in this app and over MCP;
        a change applies on their next request.
      </p>
      <AddUser onAdded={load} />

      {loadError && <p className="problems" role="alert">{loadError}</p>}
      {!users && !loadError && <Skeleton rows={4} />}
      {users && (
        <div className="schema-scroll users-table">
          <table aria-label="People">
            <thead>
              <tr><th scope="col">Email</th><th scope="col">Name</th><th scope="col">Role</th><th scope="col">Access</th><th scope="col">Last sign-in</th></tr>
            </thead>
            <tbody>
              {users.length === 0 && <tr><td colSpan={5}>Nobody yet. Add someone above.</td></tr>}
              {users.map((u) => {
                const why = reason(u);
                const whyId = why ? `user-${u.id}-why` : undefined;
                const locked = Boolean(why);
                // while saving, the controls stay focusable (aria-disabled) and ignore input, so focus is not lost
                const working = busy === u.id;
                const shown = staged[u.id] ?? u.role;
                return (
                  <tr key={u.id} data-active={u.active}>
                    <td>
                      <span>{u.email}</span>
                      {why && <span id={whyId} className="users-why">{why}</span>}
                      {rowError?.id === u.id && <span className="users-error" role="alert">{rowError.message}</span>}
                    </td>
                    <td>{u.name || <span className="muted">–</span>}</td>
                    <td>
                      <div className="users-role">
                        <select id={`user-${u.id}-role`} aria-label={`Role of ${u.email}`} aria-describedby={whyId} value={shown}
                          disabled={locked} aria-disabled={working ? "true" : undefined}
                          onChange={(e) => {
                            if (working) return;
                            const role = e.target.value as Role;
                            setConfirming(null);
                            setStaged((s) => { const n = { ...s }; if (role === u.role) delete n[u.id]; else n[u.id] = role; return n; });
                          }}>
                          {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                        </select>
                        {!locked && (
                          <Button size="sm" aria-label={`Save role of ${u.email}`} disabled={shown === u.role}
                            aria-disabled={working ? "true" : undefined} onClick={() => { if (!working) save(u); }}>
                            {working && staged[u.id] ? "Saving…" : "Save"}
                          </Button>
                        )}
                      </div>
                      {confirming === u.id && (
                        <div className="users-confirm" role="group" aria-label={`Confirm admin for ${u.email}`}>
                          <span>Make {u.email} an admin? Admins can manage people and approve role changes.</span>
                          <Button size="sm" variant="primary" autoFocus onClick={() => change(u, { role: "admin" })}>Make admin</Button>
                          <Button size="sm" variant="ghost" onClick={() => { setConfirming(null); focusRole(u.id); }}>Cancel</Button>
                        </div>
                      )}
                    </td>
                    <td>
                      <label className="users-switch">
                        <input type="checkbox" role="switch" aria-label={`${u.email} can sign in`} aria-describedby={whyId}
                          checked={u.active} disabled={locked} aria-disabled={working ? "true" : undefined}
                          onChange={(e) => { if (!working) change(u, { active: e.target.checked }); }} />
                        <span aria-hidden="true">{u.active ? "On" : "Off"}</span>
                      </label>
                    </td>
                    <td className="muted small">{lastSignIn(u.lastLoginAt)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
