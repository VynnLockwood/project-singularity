"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { Change, Me } from "@/lib/api";
import { flare, setActivity } from "@/lib/activity";
import { canDecide } from "@/lib/changes";
import { viewLabel } from "@/lib/nav";
import { useShortcut } from "@/lib/shortcuts";
import { activityLevel, useChanges } from "@/lib/useChanges";
import { useRoute } from "@/lib/useRoute";
import { ToastProvider, useToast } from "@/components/ui";
import { ChatView } from "./ChatView";
import { CommandPalette } from "./CommandPalette";
import { ConnectView } from "./ConnectView";
import { DatabaseView } from "./DatabaseView";
import { DataView } from "./DataView";
import { DuplicatesView } from "./DuplicatesView";
import { NotFound } from "./NotFound";
import { QueueView } from "./QueueView";
import { Rail } from "./Rail";
import { ReviewTray } from "./ReviewTray";
import { ScriptsTab } from "./ScriptsTab";
import { SettingsView } from "./SettingsView";
import { TopBar } from "./TopBar";

interface Props { me: Me; onLogout: () => void }

export function AppShell(props: Props) {
  return (
    <ToastProvider>
      <Shell {...props} />
    </ToastProvider>
  );
}

function Shell({ me, onLogout }: Props) {
  const [route, navigate] = useRoute();
  const toast = useToast();
  const [trayOpen, setTrayOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [draft, setDraft] = useState("");
  /** Changes proposed from this browser: their owner is already looking at them, so they get no "needs review" toast. */
  const own = useRef(new Set<number>());
  const openTray = useCallback(() => setTrayOpen(true), []);
  const openPalette = useCallback(() => setPaletteOpen(true), []);
  const closePalette = useCallback(() => setPaletteOpen(false), []);

  const changes = useChanges(me, (change, prev) => {
    const mineToDecide = canDecide(me, change);
    if (mineToDecide && !prev && !own.current.has(change.id)) {
      toast.info(`Change #${change.id} needs your review`, { action: { label: "Review", onClick: openTray } });
    }
    if (prev?.status === "pending" && change.status === "applied") flare();
    if (change.requester === me.email && prev?.status === "pending" && change.status !== "pending") {
      if (change.status === "applied") toast.success(`Change #${change.id} applied`);
      else if (change.status === "rejected" || change.status === "failed") toast.error(`Change #${change.id} ${change.status}`);
    }
  });
  const { live, mine, online, deciding, schemaKey } = changes;

  // A change made here is the next thing its author wants to act on: open the tray for it.
  const onProposed = useCallback((c: Change) => {
    own.current.add(c.id);
    changes.add(c);
    if (canDecide(me, c)) setTrayOpen(true);
  }, [changes, me]);

  const wasOnline = useRef<boolean | null>(null);
  useEffect(() => {
    if (wasOnline.current === false && online) toast.info("Reconnected");
    if (wasOnline.current !== null || online) wasOnline.current = online;
  }, [online, toast]);

  useEffect(() => {
    document.title = route.view === "notfound" ? "Page not found – Singularity" : `${viewLabel(route.view)} – Singularity`;
  }, [route.view]);

  // The background is drawn by the root layout; tell it this is the app and how busy the queue is.
  useEffect(() => {
    document.documentElement.dataset.app = "true";
    return () => { delete document.documentElement.dataset.app; setActivity(0); };
  }, []);
  useEffect(() => { setActivity(activityLevel(mine.length)); }, [mine.length]);

  useShortcut("g r", openTray);
  useShortcut("mod+k", openPalette);

  const configWaiting = live.filter((c) => c.kind === "config" && c.status === "pending").length;
  const configApplied = live.filter((c) => c.kind === "config" && c.status === "applied").length;
  const appliedCount = live.filter((c) => c.status === "applied").length;
  const busy = deciding !== null;
  const view = route.view;

  return (
    <div className="shell" data-tray={trayOpen ? "open" : "closed"}>
      <a className="shell-skip" href="#content">Skip to content</a>
      <Rail me={me} route={route} navigate={navigate} waiting={mine.length} online={online} onLogout={onLogout} />
      <div className="shell-main">
        <TopBar route={route} waiting={mine.length} onOpenTray={openTray} onOpenPalette={openPalette} />
        {changes.error && <p className="problems" role="alert">{changes.error}</p>}
        <main id="content" className="content" tabIndex={-1}>
          {view === "chat" && (
            <ChatView me={me} live={live} onDecide={changes.decide} busy={busy} draft={draft} onDraftUsed={() => setDraft("")} />
          )}
          {view === "database" && (
            <DatabaseView me={me} live={live} refreshKey={schemaKey} onProposed={onProposed}
              onAskAssistant={(t) => { setDraft(t); navigate({ view: "chat" }); }} />
          )}
          {view === "data" && <DataView me={me} live={live} refreshKey={schemaKey} onProposed={onProposed} />}
          {view === "duplicates" && <DuplicatesView me={me} refreshKey={appliedCount} onProposed={onProposed} />}
          {view === "scripts" && me.role !== "viewer" && <ScriptsTab canEdit />}
          {view === "scripts" && me.role === "viewer" && <NotFound path="/scripts" onHome={() => navigate({ view: "chat" })} />}
          {view === "connect" && <ConnectView me={me} />}
          {view === "settings" && (
            <SettingsView me={me} refreshKey={configApplied} waiting={configWaiting} onOpenQueue={() => navigate({ view: "activity" })} />
          )}
          {view === "activity" && <QueueView me={me} changes={mine} onDecide={changes.decide} busy={busy} />}
          {view === "notfound" && <NotFound path={route.path} onHome={() => navigate({ view: "chat" })} />}
        </main>
      </div>
      <CommandPalette me={me} open={paletteOpen} onClose={closePalette} navigate={navigate} refreshKey={schemaKey} onLogout={onLogout} />
      <ReviewTray me={me} changes={mine} open={trayOpen} onClose={() => setTrayOpen(false)} onDecide={changes.decide} deciding={deciding} />
    </div>
  );
}
