"use client";

import { ROLES, type Me, type Role } from "@/lib/api";
import { NAV, atLeast, navFor } from "@/lib/nav";
import type { Navigate } from "@/lib/useRoute";
import { Badge, Button, Icon, Panel } from "@/components/ui";

/** The first sentence of a tool's description: enough to say what it is for. */
const firstSentence = (text: string) => (text.match(/^.*?[.!?](\s|$)/)?.[0] ?? text).trim();

interface Locked { key: string; name: string; kind: "Page" | "AI tool"; minRole: Role }

/**
 * Who you are and what your role lets you do. The pages come from the same nav table that builds the menu, the AI tools
 * from the server's own tool table (sent in /api/me), so this page cannot promise more or less than the app allows.
 */
export function ProfileView({ me, navigate }: { me: Me; navigate: Navigate }) {
  const pages = navFor(me.role).flatMap((g) => g.items);
  const tools = me.capabilities?.mcpTools;
  const summary = me.capabilities?.roleSummary ?? [];
  const rank = ROLES.indexOf(me.role);

  const locked: Locked[] = [
    ...NAV.flatMap((g) => g.items).filter((i) => !atLeast(me.role, i.minRole))
      .map((i): Locked => ({ key: `page:${i.view}`, name: i.label, kind: "Page", minRole: i.minRole })),
    ...(tools ?? []).filter((t) => !t.allowed)
      .map((t): Locked => ({ key: `tool:${t.name}`, name: t.name, kind: "AI tool", minRole: t.minRole })),
  ].sort((a, b) => ROLES.indexOf(a.minRole) - ROLES.indexOf(b.minRole));

  return (
    <div className="profile">
      <header className="profile-head">
        <div>
          <h2>{me.name || me.email}</h2>
          {me.name && <p className="muted">{me.email}</p>}
          {me.lastLoginAt && <p className="muted small">Last signed in {new Date(me.lastLoginAt).toLocaleString()}</p>}
        </div>
        <span className="profile-role" aria-label="Your role"><Badge status={me.role} /></span>
      </header>

      <Panel title="Roles">
        <p className="muted small">Each role can do everything the roles before it can. An admin sets your role.</p>
        <ol className="profile-roles" aria-label="Roles">
          {ROLES.map((r, i) => (
            <li key={r} aria-current={r === me.role ? "true" : undefined} data-included={i <= rank}>
              <strong>{r}</strong>
              {summary[i] && <span>{summary[i]}</span>}
              {r === me.role && <span className="profile-you">Your role</span>}
            </li>
          ))}
        </ol>
      </Panel>

      <Panel title="You can" actions={<Button size="sm" icon="plug" onClick={() => navigate({ view: "connect" })}>Connect an AI client</Button>}>
        <div className="profile-can">
          <section aria-labelledby="profile-pages">
            <h4 id="profile-pages">In this app</h4>
            <ul className="profile-list" aria-label="Pages you can open">
              {pages.map((p) => (
                <li key={p.view}>
                  <button type="button" className="link" data-name onClick={() => navigate({ view: p.view })}>
                    <Icon name={p.icon} />{p.label}
                  </button>
                </li>
              ))}
            </ul>
          </section>
          <section aria-labelledby="profile-tools">
            <h4 id="profile-tools">In your AI client, over MCP</h4>
            {tools ? (
              <ul className="profile-list" aria-label="AI tools you can use">
                {tools.filter((t) => t.allowed).map((t) => (
                  <li key={t.name}>
                    <code data-name>{t.name}</code>
                    {t.description && <span className="muted small">{firstSentence(t.description)}</span>}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted small">The server did not say which AI tools your role has. Reload the page; if the list stays empty, ask an admin to update the server.</p>
            )}
          </section>
        </div>
      </Panel>

      <Panel title="Needs a higher role">
        {locked.length === 0 ? (
          <p className="muted">Nothing. Your role opens every page and tool.</p>
        ) : (
          <>
            <p className="muted small">If your work needs one of these, ask an admin for the role shown.</p>
            <ul className="profile-list profile-locked" aria-label="Needs a higher role">
              {locked.map((l) => (
                <li key={l.key}>
                  {l.kind === "AI tool" ? <code data-name>{l.name}</code> : <span data-name>{l.name}</span>}
                  <span className="muted small">{l.kind}</span>
                  <span className="profile-needs">needs {l.minRole}</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </Panel>
    </div>
  );
}
