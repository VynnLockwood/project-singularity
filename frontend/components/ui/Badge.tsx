import { Icon, type IconName } from "./Icon";

export type Status = "pending" | "applied" | "rejected" | "failed" | "stale" | "reverted";
export type Tone = "wait" | "ok" | "bad" | "idle";

const META: Record<Status, { tone: Tone; icon: IconName; label: string }> = {
  pending: { tone: "wait", icon: "clock", label: "Waiting" },
  applied: { tone: "ok", icon: "check", label: "Applied" },
  rejected: { tone: "bad", icon: "x", label: "Rejected" },
  failed: { tone: "bad", icon: "alert", label: "Failed" },
  stale: { tone: "idle", icon: "refresh", label: "Out of date" },
  reverted: { tone: "idle", icon: "undo", label: "Reverted" },
};

/** The one place a status becomes a colour, an icon and a word. */
export function statusMeta(status: string): { tone: Tone; icon: IconName; label: string } {
  return META[status as Status] ?? { tone: "idle", icon: "dot", label: status };
}

export function Badge({ status, className }: { status: string; className?: string }) {
  const m = statusMeta(status);
  return (
    <span className={["ui-badge", className].filter(Boolean).join(" ")} data-tone={m.tone} data-status={status}>
      <Icon name={m.icon} size={12} />
      {m.label}
    </span>
  );
}
