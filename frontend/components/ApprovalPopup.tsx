"use client";

import type { Change, Me } from "@/lib/api";
import { canDecide } from "@/lib/changes";
import { ChangeCard } from "./ChangeCard";

interface Props {
  me: Me;
  changes: Change[];
  onDecide: (change: Change, action: "approve" | "reject") => Promise<void> | void;
  busy?: boolean;
  onSnooze: (id: number) => void;
}

/** Modal for the oldest pending change this user can decide. "Later" hides it until a new one arrives. */
export function ApprovalPopup({ me, changes, onDecide, busy, onSnooze }: Props) {
  const next = changes.find((c) => canDecide(me, c));
  if (!next) return null;
  const waiting = changes.filter((c) => canDecide(me, c)).length - 1;
  return (
    <div className="overlay" role="dialog" aria-modal="true" aria-label="Approval needed">
      <div className="modal">
        <h2 className="modal-title">Approval needed</h2>
        <ChangeCard change={next} me={me} onDecide={onDecide} busy={busy} />
        <div className="modal-foot">
          {waiting > 0 && <span className="muted small">{waiting} more waiting</span>}
          <button className="link" onClick={() => onSnooze(next.id)}>
            Later
          </button>
        </div>
      </div>
    </div>
  );
}
