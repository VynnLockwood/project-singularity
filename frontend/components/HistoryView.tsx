"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, getSchemas, listChanges, revertChange, type Change, type Me } from "@/lib/api";
import { DEFAULT_FILTERS, filtersToSearch, mergeLive, parseFilters, toApiFilters, type HistoryFilters } from "@/lib/historyFilters";
import { Badge, Button, EmptyState, Skeleton, statusMeta, useToast } from "@/components/ui";
import { ChangeCard } from "./ChangeCard";

const PAGE = 50;
const CAP = 200; // the most the server returns in one request

const STATUSES = ["pending", "applied", "rejected", "failed", "stale"] as const;
const KINDS: { value: Change["kind"]; label: string }[] = [
  { value: "schema", label: "Schema" }, { value: "data", label: "Data" }, { value: "config", label: "Settings" }, { value: "sql", label: "Raw SQL" },
];
const KIND_LABEL = Object.fromEntries(KINDS.map((k) => [k.value, k.label]));

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [["day", 86400], ["hour", 3600], ["minute", 60]];
/** "3 hours ago", "yesterday"; falls back to the raw text for something that is not a date. */
function ago(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const secs = Math.round((t - Date.now()) / 1000);
  const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  for (const [unit, size] of UNITS) if (Math.abs(secs) >= size) return rtf.format(Math.round(secs / size), unit);
  return "just now";
}

/** A revert is offered to approvers for an applied, reversible change that nothing has reverted (or is reverting) yet. */
function canRevert(c: Change, rows: Change[], me: Me): boolean {
  if (me.role !== "approver" || c.status !== "applied" || c.reversible !== true) return false;
  return !rows.some((r) => r.reverts === c.id && r.status !== "rejected" && r.status !== "failed");
}

interface Props { me: Me; live: Change[]; busy?: boolean }

/** Every change ever made, newest first, filterable. The address carries the filters. */
export function HistoryView({ me, live, busy }: Props) {
  const toast = useToast();
  const [filters, setFilters] = useState<HistoryFilters>(() => parseFilters(typeof location === "undefined" ? "" : location.search));
  const [limit, setLimit] = useState(PAGE);
  const [rows, setRows] = useState<Change[]>([]);
  const [more, setMore] = useState(false);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [confirming, setConfirming] = useState<number | null>(null);
  const [revertError, setRevertError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const [dbs, setDbs] = useState<string[]>([]);
  const request = useRef(0);
  const reverting = useRef(false);

  useEffect(() => { getSchemas().then((s) => setDbs(s.map((x) => x.name))).catch(() => {}); }, []);

  useEffect(() => {
    const id = ++request.current;
    const firstPage = limit === PAGE;
    if (firstPage) setState("loading");
    listChanges(toApiFilters(filters), limit)
      .then((r) => {
        if (id !== request.current) return; // a newer request owns the list now
        setRows(r.changes);
        setMore(r.more);
        setError("");
        setState("ready");
      })
      .catch((e) => {
        if (id !== request.current) return;
        setError(e instanceof ApiError ? e.message : "Could not load the history.");
        if (firstPage) setState("error");
      });
  }, [filters, limit, reloadKey]);

  const applyFilters = useCallback((next: HistoryFilters) => {
    setFilters(next);
    setLimit(PAGE);
    setOpen(null);
    history.replaceState(null, "", location.pathname + filtersToSearch(next));
  }, []);

  const visible = mergeLive(rows, live, filters, me);
  const dbOptions = filters.db && !dbs.includes(filters.db) ? [...dbs, filters.db] : dbs; // a dropped database still has history

  const doRevert = async (c: Change) => {
    if (reverting.current) return;
    reverting.current = true;
    setRevertError("");
    try {
      await revertChange(c.id);
      toast.success(`Change #${c.id} reverted`);
      setConfirming(null);
      setReloadKey((k) => k + 1);
    } catch (e) {
      setRevertError(e instanceof ApiError ? e.message : "Could not revert this change.");
      setConfirming(null);
    } finally {
      reverting.current = false;
    }
  };

  return (
    <div className="history">
      <div className="history-filters" role="group" aria-label="Filter history">
        <select aria-label="Status" value={filters.status} onChange={(e) => applyFilters({ ...filters, status: e.target.value as HistoryFilters["status"] })}>
          <option value="">Any status</option>
          {STATUSES.map((s) => <option key={s} value={s}>{statusMeta(s).label}</option>)}
        </select>
        <select aria-label="Kind" value={filters.kind} onChange={(e) => applyFilters({ ...filters, kind: e.target.value as HistoryFilters["kind"] })}>
          <option value="">Any kind</option>
          {KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
        </select>
        <select aria-label="Database" value={filters.db} onChange={(e) => applyFilters({ ...filters, db: e.target.value })}>
          <option value="">Any database</option>
          {dbOptions.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <label className="history-mine">
          <input type="checkbox" aria-label="Only my changes" checked={filters.mine} onChange={(e) => applyFilters({ ...filters, mine: e.target.checked })} />
          Only mine
        </label>
      </div>

      {state === "loading" && <Skeleton rows={5} />}
      {state === "error" && (
        <div>
          <p className="problems" role="alert">{error}</p>
          <Button onClick={() => setReloadKey((k) => k + 1)}>Try again</Button>
        </div>
      )}
      {state === "ready" && visible.length === 0 && (
        <EmptyState icon="inbox" title="No changes match these filters.">
          <Button onClick={() => applyFilters(DEFAULT_FILTERS)}>Clear filters</Button>
        </EmptyState>
      )}
      {state === "ready" && visible.length > 0 && (
        <ul className="history-list">
          {visible.map((c) => {
            const expanded = open === c.id;
            return (
              <li key={c.id} className="history-item">
                <button className="history-row" aria-expanded={expanded} onClick={() => { setOpen(expanded ? null : c.id); setConfirming(null); setRevertError(""); }}>
                  <strong>#{c.id}</strong>
                  <Badge status={c.status} />
                  <span>{KIND_LABEL[c.kind] ?? c.kind}</span>
                  <span className="muted">{c.schema ?? "master"}</span>
                  <span className="muted">{c.tables.slice(0, 2).join(", ")}{c.tables.length > 2 ? ` +${c.tables.length - 2}` : ""}</span>
                  <span className="muted">{c.requester}</span>
                  <time className="muted" dateTime={c.createdAt} title={new Date(c.createdAt).toLocaleString()}>{ago(c.createdAt)}</time>
                  <span className="history-prompt">{c.prompt.split("\n")[0]}</span>
                </button>
                {expanded && (
                  <div className="history-detail">
                    <ChangeCard change={c} me={me} busy={busy} />
                    {canRevert(c, visible, me) && (confirming === c.id ? (
                      <p className="actions">
                        <span>Revert change #{c.id}?</span>
                        <Button variant="danger" onClick={() => doRevert(c)}>Yes, revert</Button>
                        <Button onClick={() => setConfirming(null)}>Cancel</Button>
                      </p>
                    ) : (
                      <Button icon="undo" onClick={() => { setConfirming(c.id); setRevertError(""); }}>Revert…</Button>
                    ))}
                    {revertError && <p className="problems" role="alert">{revertError}</p>}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {state === "ready" && error && <p className="problems" role="alert">{error}</p>}
      {state === "ready" && more && limit < CAP && <Button onClick={() => setLimit((l) => Math.min(l + PAGE, CAP))}>Show more</Button>}
      {state === "ready" && more && limit >= CAP && (
        <p className="muted">Showing the latest {CAP}. Narrow the filters to see older changes.</p>
      )}
    </div>
  );
}
