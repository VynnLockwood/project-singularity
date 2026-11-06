"use client";

import { useCallback, useEffect, useState } from "react";
import { decide, getQueue, ApiError, type Change, type Me } from "@/lib/api";
import { canDecide, upsert } from "@/lib/changes";
import { connectEvents } from "@/lib/events";
import { ApprovalPopup } from "./ApprovalPopup";
import { ChatView } from "./ChatView";
import { DataView } from "./DataView";
import { DuplicatesView } from "./DuplicatesView";
import { ConnectView } from "./ConnectView";
import { SettingsView } from "./SettingsView";
import { QueueView } from "./QueueView";
import { SearchBox } from "./SearchBox";
import { SchemaView } from "./SchemaView";

type Tab = "chat" | "schema" | "data" | "duplicates" | "connect" | "settings" | "queue";

export function AppShell({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const [tab, setTab] = useState<Tab>("chat");
  const [live, setLive] = useState<Change[]>([]);
  const [snoozed, setSnoozed] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [schemaKey, setSchemaKey] = useState(0);
  const [online, setOnline] = useState(false);
  const [draft, setDraft] = useState("");

  // What is waiting for this person now, then keep it fresh from the event stream.
  useEffect(() => {
    getQueue().then((q) => { if (Array.isArray(q)) setLive((l) => q.reduce(upsert, l)); }).catch(() => {});
    return connectEvents(
      (e) => {
        setLive((l) => upsert(l, e.change));
        setSnoozed((s) => { const n = new Set(s); n.delete(e.change.id); return n; });
        if (e.change.status === "applied" && e.change.kind === "schema") setSchemaKey((k) => k + 1);
      },
      setOnline,
    );
  }, []);

  const onDecide = useCallback(async (change: Change, action: "approve" | "reject") => {
    setBusy(true);
    setError("");
    try {
      const updated = await decide(change.id, action);
      setLive((l) => upsert(l, { ...change, ...updated }));
      if (updated.status === "applied" && updated.kind === "schema") setSchemaKey((k) => k + 1);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not record the decision.");
    } finally {
      setBusy(false);
    }
  }, []);

  const appliedCount = live.filter((c) => c.status === "applied").length;
  const mine = live.filter((c) => canDecide(me, c));
  const popupChanges = mine.filter((c) => !snoozed.has(c.id));

  return (
    <div className="shell">
      <header className="topbar">
        <span className="brand">Singularity</span>
        <nav aria-label="Main">
          {(["chat", "schema", "data", "duplicates", "connect", "settings", ...(me.role === "viewer" ? [] : ["queue"])] as Tab[]).map((t) => (
            <button key={t} className="tab" data-active={tab === t} onClick={() => setTab(t)}>
              {t === "chat" ? "Chat" : t === "schema" ? "Schema" : t === "data" ? "Data" : t === "duplicates" ? "Duplicates" : t === "connect" ? "Connect" : t === "settings" ? "Settings" : `Queue${mine.length ? ` (${mine.length})` : ""}`}
            </button>
          ))}
        </nav>
        <span className="muted small" title={online ? "Live updates on" : "Reconnecting…"}>
          {online ? "● live" : "○ offline"}
        </span>
        <span className="muted small">{me.email} · {me.role}</span>
        <button className="link" onClick={onLogout}>Sign out</button>
      </header>
      {error && <p className="problems" role="alert">{error}</p>}
      <main className="content">
        {tab === "chat" && <ChatView me={me} live={live} onDecide={onDecide} busy={busy} draft={draft} onDraftUsed={() => setDraft("")} />}
        {tab === "schema" && (
          <>
            <SearchBox canPropose={me.role !== "viewer"} canReindex={me.role === "approver"} onAskAssistant={(t) => { setDraft(t); setTab("chat"); }} />
            <SchemaView refreshKey={schemaKey} />
          </>
        )}
        {tab === "data" && (
          <DataView me={me} live={live} refreshKey={schemaKey}
            onProposed={(c) => setLive((l) => upsert(l, c))} />
        )}
        {tab === "duplicates" && (
          <DuplicatesView me={me} refreshKey={appliedCount} onProposed={(c) => setLive((l) => upsert(l, c))} />
        )}
        {tab === "connect" && <ConnectView me={me} />}
        {tab === "settings" && <SettingsView me={me} />}
        {tab === "queue" && <QueueView me={me} changes={mine} onDecide={onDecide} busy={busy} />}
      </main>
      <ApprovalPopup
        me={me}
        changes={popupChanges}
        onDecide={onDecide}
        busy={busy}
        onSnooze={(id) => setSnoozed((s) => new Set(s).add(id))}
      />
    </div>
  );
}
