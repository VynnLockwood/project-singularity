import type { Change, Me } from "./api";

/** Mirrors the server's rule: approvers decide anything; editors decide their own data changes. */
export function canDecide(me: Me, change: Change): boolean {
  if (change.status !== "pending") return false;
  if (me.role === "approver") return true;
  return me.role === "editor" && change.kind === "data" && change.requester === me.email;
}

export type DiffKind = "add" | "remove" | "change";

export function diffKind(line: string): DiffKind {
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "remove";
  return "change";
}

export function warnings(change: Change) {
  return (change.check.violations ?? []).filter((v) => v.severity === "warn");
}

/** Latest event wins; keeps the list ordered by id. */
export function upsert(list: Change[], change: Change): Change[] {
  const rest = list.filter((c) => c.id !== change.id);
  return [...rest, change].sort((a, b) => a.id - b.id);
}
