"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ApiError, dismissDuplicate, getDuplicate, getGraph, listDuplicates, mergeDuplicate, reopenDuplicate, scanDuplicates,
  type Change, type DuplicateDetail, type DuplicateSummary, type Me,
} from "@/lib/api";
import { show } from "@/lib/grid";

interface Props {
  me: Me;
  /** Bumps when any change is applied, so merged groups refresh. */
  refreshKey: number;
  onProposed?: (change: Change) => void;
}

const LABEL: Record<DuplicateSummary["status"], string> = {
  open: "open", dismissed: "not duplicates", merge_pending: "merge waiting for approval", merged: "merged",
};

export function DuplicatesView({ me, refreshKey, onProposed }: Props) {
  const canEdit = me.role !== "viewer";
  const [tables, setTables] = useState<string[]>([]);
  const [table, setTable] = useState("");
  const [groups, setGroups] = useState<DuplicateSummary[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [showAll, setShowAll] = useState(false);
  const [open, setOpen] = useState<DuplicateDetail | null>(null);
  const [survivor, setSurvivor] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  const fail = (e: unknown) => setError(e instanceof ApiError ? e.message : "Something went wrong.");

  const load = useCallback(async (t: string) => {
    const r = await listDuplicates(t || undefined);
    setGroups(r.groups);
    setCounts(r.tables);
  }, []);

  useEffect(() => {
    getGraph().then((g) => {
      const names = g.tables.map((x) => x.name);
      setTables(names);
      setTable((cur) => (cur && names.includes(cur) ? cur : names[0] ?? ""));
    }).catch(() => setError("Could not load the tables."));
  }, [refreshKey]);

  useEffect(() => {
    if (table) load(table).catch(fail);
    setOpen(null);
  }, [table, load, refreshKey]);

  const scan = async () => {
    setBusy(true); setError(""); setNote("");
    try {
      const r = await scanDuplicates(table);
      setNote(`Scanned ${r.scanned} records by ${r.fields.join(", ")}: ${r.groups} group(s), ${r.new} new.${r.truncated ? " Only the first records were scanned (row cap)." : ""}`);
      await load(table);
    } catch (e) { fail(e); } finally { setBusy(false); }
  };

  const view = async (g: DuplicateSummary) => {
    setError("");
    try {
      const d = await getDuplicate(g.id);
      setOpen(d);
      setSurvivor(d.memberIds[0] ?? null);
    } catch (e) { fail(e); }
  };

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await fn(); await load(table); } catch (e) { fail(e); } finally { setBusy(false); }
  };

  const merge = () =>
    act(async () => {
      if (!open || survivor === null) return;
      const r = await mergeDuplicate(open.id, survivor);
      onProposed?.(r.change);
      setNote(`Merge proposed as change #${r.change.id}. ${r.change.status === "rejected" ? "The rules gate blocked it." : "Approve it in the popup."}`);
      setOpen(null);
    });

  const visible = groups.filter((g) => showAll || g.status === "open" || g.status === "merge_pending");
  const columns = open ? Object.keys(open.members[0] ?? {}).filter((c) => !(open.hiddenColumns ?? []).includes(c)) : [];

  return (
    <div className="duplicates">
      <div className="grid-bar">
        <label className="small">
          Table{" "}
          <select aria-label="Table" value={table} onChange={(e) => setTable(e.target.value)}>
            {tables.map((t) => <option key={t} value={t}>{t}{counts[t] ? ` (${counts[t]})` : ""}</option>)}
          </select>
        </label>
        {canEdit && <button className="button" disabled={busy || !table} onClick={scan}>Scan for duplicates</button>}
        <label className="small">
          <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} /> show dismissed and merged
        </label>
      </div>
      {note && <p className="muted small">{note}</p>}
      {error && <p className="problems" role="alert">{error}</p>}

      {visible.length === 0 && <p className="muted">No duplicate groups{canEdit ? ". Run a scan to look for them." : "."}</p>}
      <ul className="dup-list" aria-label="Duplicate groups">
        {visible.map((g) => (
          <li key={g.id} data-status={g.status}>
            <button className="link" onClick={() => view(g)}>
              {g.memberIds.length} records · {Math.round(g.score * 100)}% · by {g.fields.join(", ")}
            </button>
            <span className="badge" data-status={g.status}>{LABEL[g.status]}</span>
            {canEdit && g.status === "open" && (
              <button className="link" disabled={busy} onClick={() => act(() => dismissDuplicate(g.id))}>Not duplicates</button>
            )}
            {canEdit && g.status === "dismissed" && (
              <button className="link" disabled={busy} onClick={() => act(() => reopenDuplicate(g.id))}>Reopen</button>
            )}
          </li>
        ))}
      </ul>

      {open && (
        <section className="change" aria-label="Duplicate group">
          <header className="change-head">
            <strong>{open.memberIds.length} records that look the same</strong>
            <button className="link" onClick={() => setOpen(null)}>Close</button>
          </header>
          <div className="grid-scroll">
            <table className="grid">
              <thead>
                <tr>{canEdit && <th>keep</th>}{columns.map((c) => <th key={c}>{c}</th>)}</tr>
              </thead>
              <tbody>
                {open.members.map((m) => {
                  const mid = m[open.primaryKey ?? "id"] as number;
                  return (
                  <tr key={String(mid)} data-removed={survivor !== null && survivor !== mid && canEdit}>
                    {canEdit && (
                      <td>
                        <input type="radio" name="survivor" aria-label={`keep record ${mid}`} checked={survivor === mid}
                          onChange={() => setSurvivor(mid)} />
                      </td>
                    )}
                    {columns.map((c) => <td key={c}>{show(m[c])}</td>)}
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {open.referencedBy.length > 0 && (
            <p className="muted small">
              Records pointing at these will be moved to the one you keep: {open.referencedBy.map((r) => `${r.table}.${r.column}`).join(", ")}.
            </p>
          )}
          {canEdit && open.status === "open" && (
            <div className="actions">
              <button className="button" disabled={busy || survivor === null} onClick={merge}>Merge into the selected record</button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
