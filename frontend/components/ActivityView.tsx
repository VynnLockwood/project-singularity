"use client";

import type { Change, Me } from "@/lib/api";
import type { Navigate } from "@/lib/useRoute";
import { HistoryView } from "./HistoryView";
import { QueueView } from "./QueueView";

interface Props {
  me: Me;
  tab: "queue" | "history";
  navigate: Navigate;
  /** Pending changes this person can decide. */
  pending: Change[];
  live: Change[];
  onDecide: (change: Change, action: "approve" | "reject") => Promise<void> | void;
  busy?: boolean;
}

/** Activity: what waits for a decision (Queue) and what already happened (History). */
export function ActivityView({ me, tab, navigate, pending, live, onDecide, busy }: Props) {
  return (
    <div className="activity">
      <div className="db-tabs" role="tablist" aria-label="Activity sections">
        <button role="tab" className="tab" aria-selected={tab === "queue"} data-active={tab === "queue"} onClick={() => navigate({ view: "activity" })}>
          Queue{pending.length > 0 && <span className="tab-count">{pending.length}</span>}
        </button>
        <button role="tab" className="tab" aria-selected={tab === "history"} data-active={tab === "history"} onClick={() => navigate({ view: "activity", tab: "history" })}>
          History
        </button>
      </div>
      {tab === "queue" ? <QueueView me={me} changes={pending} onDecide={onDecide} busy={busy} /> : <HistoryView me={me} live={live} busy={busy} />}
    </div>
  );
}
