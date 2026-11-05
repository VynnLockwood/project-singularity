"use client";

import type { Change, Me } from "@/lib/api";
import { ChangeCard } from "./ChangeCard";

interface Props {
  me: Me;
  changes: Change[];
  onDecide: (change: Change, action: "approve" | "reject") => Promise<void> | void;
  busy?: boolean;
}

export function QueueView({ me, changes, onDecide, busy }: Props) {
  const pending = changes.filter((c) => c.status === "pending");
  if (pending.length === 0) return <p className="muted">Nothing is waiting for review.</p>;
  return (
    <div className="queue">
      {pending.map((c) => (
        <ChangeCard key={c.id} change={c} me={me} onDecide={onDecide} busy={busy} />
      ))}
    </div>
  );
}
