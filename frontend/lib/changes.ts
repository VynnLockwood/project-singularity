import type { Change, Me } from "./api";
import { atLeast } from "./nav";

/** A settings change that grants, changes or removes a role: the server lets only an admin decide it. */
export function changesRoles(change: Change): boolean {
  return change.kind === "config" && (change.operations ?? []).some((o) => o.op === "set_user_role");
}

/**
 * Mirrors the server's rule (approvals/service.py can_decide): role changes need an admin; otherwise approvers
 * (and admins) decide anything and editors decide their own data changes.
 */
export function canDecide(me: Me, change: Change): boolean {
  if (change.status !== "pending") return false;
  if (changesRoles(change)) return me.role === "admin";
  if (atLeast(me.role, "approver")) return true;
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
