"use client";

import type { Change, Me } from "@/lib/api";
import { Button, EmptyState, Tray } from "@/components/ui";
import { ChangeCard } from "./ChangeCard";

interface Props {
  me: Me;
  /** Pending changes this person can decide. */
  changes: Change[];
  open: boolean;
  onClose: () => void;
  onDecide: (change: Change, action: "approve" | "reject") => Promise<void> | void;
  /** Id of the change a decision is in flight for. */
  deciding: number | null;
}

/** Everything waiting for this person's decision, beside the page instead of on top of it. */
export function ReviewTray({ me, changes, open, onClose, onDecide, deciding }: Props) {
  const waiting = [...changes].sort((a, b) => a.id - b.id);
  return (
    <Tray open={open} onClose={onClose} label="Needs review">
      <header className="ui-tray-head">
        <h2>Needs review</h2>
        {waiting.length > 0 && <span className="muted small">{waiting.length} waiting</span>}
        <Button variant="ghost" size="sm" icon="x" aria-label="Close review" onClick={onClose} />
      </header>
      <div className="ui-tray-body">
        {waiting.length === 0 ? (
          <EmptyState icon="inbox" title="Nothing is waiting for review.">
            New changes show up here as they arrive.
          </EmptyState>
        ) : (
          waiting.map((c) => <ChangeCard key={c.id} change={c} me={me} onDecide={onDecide} deciding={deciding === c.id} />)
        )}
      </div>
    </Tray>
  );
}
