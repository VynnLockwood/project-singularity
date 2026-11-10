"use client";

import type { Change, Me } from "@/lib/api";
import { EmptyState } from "@/components/ui";
import { ChangeCard } from "./ChangeCard";

interface Props {
  me: Me;
  changes: Change[];
  onDecide: (change: Change, action: "approve" | "reject") => Promise<void> | void;
  busy?: boolean;
}

export function QueueView({ me, changes, onDecide, busy }: Props) {
  const pending = changes.filter((c) => c.status === "pending");
  if (pending.length === 0) {
    return (
      <EmptyState icon="inbox" title="Nothing is waiting for review.">
        Changes that need your decision show up here and in the review tray.
      </EmptyState>
    );
  }
  return (
    <div className="queue">
      {pending.map((c) => (
        <ChangeCard key={c.id} change={c} me={me} onDecide={onDecide} busy={busy} />
      ))}
    </div>
  );
}
