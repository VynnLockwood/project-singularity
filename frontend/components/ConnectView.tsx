"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, createToken, listTokens, revokeToken, type McpToken, type Me } from "@/lib/api";
import { snippets, type Snippets } from "@/lib/connect";

function Copy({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="link"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); }).catch(() => {});
      }}
    >
      {done ? "Copied" : label}
    </button>
  );
}

/** Make a connection link for Claude, Gemini or any MCP client, and manage the tokens behind them. */
export function ConnectView({ me }: { me: Me }) {
  const [tokens, setTokens] = useState<McpToken[]>([]);
  const [name, setName] = useState("");
  const [fresh, setFresh] = useState<Snippets | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const allowed = me.role !== "viewer";

  const load = useCallback(() => listTokens().then(setTokens).catch(() => setError("Could not load tokens.")), []);
  useEffect(() => { if (allowed) load(); }, [allowed, load]);

  const origin = typeof window !== "undefined" ? window.location.origin : "";

  const make = async () => {
    setBusy(true); setError("");
    try {
      const r = await createToken(name);
      setFresh(snippets(origin, r.token));
      setName("");
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not create the link.");
    } finally { setBusy(false); }
  };

  const revoke = async (id: number) => {
    setError("");
    try { await revokeToken(id); await load(); } catch { setError("Could not revoke that token."); }
  };

  if (!allowed) return <p className="muted">Viewers cannot connect AI clients. Ask an approver for the editor role.</p>;

  return (
    <div className="connect">
      <p className="muted">
        Connect Claude, Gemini or any MCP client to this database. Your client gets the same tools as your role,
        and every change still goes through the rules gate and the approval popup.
      </p>

      <div className="grid-bar">
        <input aria-label="Connection name" placeholder="Name this connection, e.g. Claude on my laptop" value={name}
          onChange={(e) => setName(e.target.value)} maxLength={64} />
        <button className="button" disabled={busy || !name.trim()} onClick={make}>Create connection link</button>
      </div>
      {error && <p className="problems" role="alert">{error}</p>}

      {fresh && (
        <section className="change" aria-label="New connection">
          <p className="small"><strong>Copy this now.</strong> The link is a password: it is shown once and only a hash is stored.</p>

          <h3>Claude (claude.ai, Desktop, mobile)</h3>
          <p className="small muted">Settings, Connectors, Add custom connector, then paste the link.</p>
          <pre className="code" data-testid="link">{fresh.link}</pre>
          <Copy text={fresh.link} label="Copy link" />

          <h3>Gemini CLI</h3>
          <p className="small muted">Add this to <code>~/.gemini/settings.json</code>.</p>
          <pre className="code">{fresh.gemini}</pre>
          <Copy text={fresh.gemini} label="Copy settings" />

          <h3>Claude Code</h3>
          <pre className="code">{fresh.claudeCode}</pre>
          <Copy text={fresh.claudeCode} label="Copy command" />
          <p className="small muted">The link puts the token in the address, so it can show up in server logs. Prefer the header forms when the client supports them, and revoke a link you no longer use.</p>
        </section>
      )}

      <h3>Your connections</h3>
      {tokens.length === 0 ? <p className="muted">None yet.</p> : (
        <ul className="dup-list" aria-label="Connections">
          {tokens.map((t) => (
            <li key={t.id}>
              <strong>{t.name}</strong>
              <span className="muted small">{t.prefix}… · {t.lastUsedAt ? `used ${new Date(t.lastUsedAt).toLocaleString()}` : "never used"}</span>
              <button className="link" onClick={() => revoke(t.id)} aria-label={`Revoke ${t.name}`}>Revoke</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
