import type { Change, ChangeFilters, ChangeStatus, Me } from "./api";

export interface HistoryFilters { status: ChangeStatus | ""; kind: Change["kind"] | ""; db: string; mine: boolean }

export const DEFAULT_FILTERS: HistoryFilters = { status: "", kind: "", db: "", mine: false };

const STATUSES: readonly string[] = ["pending", "applied", "rejected", "failed", "stale"];
const KINDS: readonly string[] = ["schema", "data", "config", "sql"];

/** Reads the filters out of a query string. Anything unknown means "any": a hand-edited link never breaks the page. */
export function parseFilters(search: string): HistoryFilters {
  const q = new URLSearchParams(search);
  const status = q.get("status") ?? "";
  const kind = q.get("kind") ?? "";
  return {
    status: STATUSES.includes(status) ? (status as ChangeStatus) : "",
    kind: KINDS.includes(kind) ? (kind as Change["kind"]) : "",
    db: q.get("db") ?? "",
    mine: q.get("mine") === "1",
  };
}

export function filtersToSearch(f: HistoryFilters): string {
  const q = new URLSearchParams();
  if (f.status) q.set("status", f.status);
  if (f.kind) q.set("kind", f.kind);
  if (f.db) q.set("db", f.db);
  if (f.mine) q.set("mine", "1");
  const s = q.toString();
  return s ? `?${s}` : "";
}

export function toApiFilters(f: HistoryFilters): ChangeFilters {
  return {
    ...(f.status ? { status: f.status } : {}),
    ...(f.kind ? { kind: f.kind } : {}),
    ...(f.db ? { schema: f.db } : {}),
    ...(f.mine ? { mine: true } : {}),
  };
}

export function matches(c: Change, f: HistoryFilters, me: Me): boolean {
  if (f.status && c.status !== f.status) return false;
  if (f.kind && c.kind !== f.kind) return false;
  if (f.db && (c.schema ?? "master") !== f.db) return false;
  if (f.mine && c.requester !== me.email) return false;
  return true;
}

/** The loaded rows with fresher live copies swapped in, and newer matching live changes added. Newest first. */
export function mergeLive(rows: Change[], live: Change[], f: HistoryFilters, me: Me): Change[] {
  const liveById = new Map(live.map((c) => [c.id, c]));
  const known = new Set(rows.map((c) => c.id));
  const top = rows.reduce((m, c) => Math.max(m, c.id), 0);
  const fresh = live.filter((c) => !known.has(c.id) && c.id > top && matches(c, f, me));
  const unique = [...new Map(fresh.map((c) => [c.id, c])).values()];
  return [...rows.map((c) => liveById.get(c.id) ?? c), ...unique].sort((a, b) => b.id - a.id);
}
