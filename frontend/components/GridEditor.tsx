"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError, csvUrl, getOptions, getRows, proposeChange,
  type Change, type GridColumn, type Me, type RefOptions, type RowsPage,
} from "@/lib/api";
import {
  buildOperations, cellText, draftSize, emptyDraft, validate, type Draft,
} from "@/lib/grid";

import { EMPTY_VIEW, type DataView } from "@/lib/dataRoute";
import { Button, EmptyState, Icon, ProgressBar, Skeleton } from "@/components/ui";
import { ColumnStats } from "./ColumnStats";
import { FilterBar } from "./FilterBar";

const PAGE_SIZES = [25, 50, 100, 200] as const;

interface Props {
  me: Me;
  table: string;
  /** Latest changes (kept fresh by WebSocket events); an applied data change to this table reloads the page. */
  live: Change[];
  /** Called with the proposal once saved, so the app can show the approval popup right away. */
  onProposed?: (change: Change) => void;
  /** Told when a page fetch starts and ends. The grid shows its own progress; nothing covers the app. */
  onBusy?: (b: boolean) => void;
  /** Database the table lives in; the default one when omitted. */
  schema?: string;
  /** Sort, filters and inactive. When given, the parent owns them (the Data tab keeps them in the address). */
  view?: DataView;
  onViewChange?: (next: DataView) => void;
}

export function GridEditor({ me, table, live, onProposed, onBusy, schema, view, onViewChange }: Props) {
  const canEdit = me.role !== "viewer";
  const [page, setPage] = useState<RowsPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [inner, setInner] = useState<DataView>(view ?? EMPTY_VIEW);
  const [pageSize, setPageSize] = useState(50);
  const [draft, setDraft] = useState<Draft>(emptyDraft());
  const [refs, setRefs] = useState<Record<string, RefOptions>>({});
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [reload, setReload] = useState(0);
  const [showStats, setShowStats] = useState(false);
  const [showRaw, setShowRaw] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState("");

  const v = view ?? inner;
  const viewKey = JSON.stringify(v);
  // Any change of view goes back to the first page. Pending edits belong to rows, so they stay.
  const setView = (next: DataView) => { setInner(next); onViewChange?.(next); setOffset(0); };

  const appliedHere = live.filter((c) => c.status === "applied" && c.kind === "data" && c.tables.includes(table)).length;

  // New table: start clean.
  useEffect(() => {
    setOffset(0); setInner(EMPTY_VIEW);
    setDraft(emptyDraft()); setNote(""); setError(""); setExportError("");
  }, [table]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    onBusy?.(true);
    const query = { offset, limit: pageSize, sort: v.sort ?? undefined, dir: v.dir, filters: v.filters, inactive: v.inactive };
    (schema ? getRows(table, query, schema) : getRows(table, query))
      .then((p) => { if (!cancelled) { setPage(p); setError(""); setLoadFailed(false); } })
      .catch((e) => { if (!cancelled) { setError(e instanceof ApiError ? e.message : "Could not load rows."); setLoadFailed(true); } })
      .finally(() => { onBusy?.(false); if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [table, schema, offset, pageSize, viewKey, reload, appliedHere]);

  // Load dropdown options for every reference column.
  useEffect(() => {
    if (!page) return;
    for (const c of page.columns) {
      const ref = c.references?.table;
      if (ref && !refs[ref]) (schema ? getOptions(ref, schema) : getOptions(ref)).then((o) => setRefs((r) => ({ ...r, [ref]: o }))).catch(() => {});
    }
  }, [page, refs]);

  const columns = page?.columns ?? [];
  const problems = useMemo(() => validate(columns, draft), [columns, draft]);
  const pending = draftSize(draft);

  const setCell = (id: number | string, name: string, raw: string) =>
    setDraft((d) => {
      if (typeof id === "string" && id.startsWith("new")) {
        const i = Number(id.slice(3));
        return { ...d, added: d.added.map((r, j) => (j === i ? { ...r, [name]: raw } : r)) };
      }
      return { ...d, edits: { ...d.edits, [String(id)]: { ...d.edits[String(id)], [name]: raw } } };
    });

  const toggleRemove = (id: number) =>
    setDraft((d) => ({ ...d, removed: d.removed.includes(id) ? d.removed.filter((x) => x !== id) : [...d.removed, id] }));

  // asc, then desc, then no sort.
  const sortBy = (name: string) => {
    if (v.sort !== name) setView({ ...v, sort: name, dir: "asc" });
    else if (v.dir === "asc") setView({ ...v, dir: "desc" });
    else setView({ ...v, sort: null, dir: "asc" });
  };

  const exportCsv = async () => {
    setExporting(true); setExportError("");
    try {
      const res = await fetch(csvUrl(table, schema ?? "master", v), { credentials: "same-origin" });
      if (!res.ok) {
        let message = "Could not export.";
        try { message = (await res.json()).error || message; } catch { /* not JSON */ }
        setExportError(message);
        return;
      }
      const blob = await res.blob();
      const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") ?? "")?.[1] ?? `${table}.csv`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = name;
      document.body.append(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch {
      setExportError("Could not export. Check your connection and try again.");
    } finally {
      setExporting(false);
    }
  };

  const save = useCallback(async () => {
    if (!page) return;
    const ops = buildOperations(table, page.columns, draft, page.primaryKey ?? "id");
    if (!ops.length) return;
    setSaving(true); setError(""); setNote("");
    try {
      const change = await proposeChange(ops, `Grid edit on ${table}`);
      setNote(`Saved as change #${change.id}. ${change.status === "pending" ? "Waiting for approval." : change.status}`);
      if (change.status === "rejected") setError("The rules gate blocked this change. See the details in the approval card.");
      else setDraft(emptyDraft());
      onProposed?.(change);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not save.");
    } finally {
      setSaving(false);
    }
  }, [page, draft, table, onProposed]);

  const input = (c: GridColumn, key: string | number, value: string, edited: boolean) => {
    const bad = problems[`${key}:${c.name}`];
    const common = { "aria-label": `${c.name} ${key}`, "aria-invalid": bad ? true : undefined, "data-edited": edited, title: bad };
    const ref = c.references ? refs[c.references.table] : undefined;
    const problem = bad ? <span className="cell-problem"><Icon name="alert" size={12} />{bad}</span> : null;
    if (ref) {
      // ponytail: labels toggle drives display text here too; option VALUES stay ids so editing still saves the FK
      const shown = displayText(c, value);
      return (
        <>
          <select {...common} value={value} title={bad || (shown !== value ? value : undefined)} onChange={(e) => setCell(key, c.name, e.target.value)}>
            <option value="">{c.nullable ? "—" : "choose…"}</option>
            {ref.options.map((o) => <option key={String(o.id)} value={String(o.id)}>{displayText(c, String(o.id))}</option>)}
            {value && !ref.options.some((o) => String(o.id) === value) && <option value={value}>{shown}</option>}
          </select>
          {problem}
        </>
      );
    }
    if (c.type === "boolean") {
      return (
        <>
          <select {...common} value={value} onChange={(e) => setCell(key, c.name, e.target.value)}>
            <option value="">{c.nullable ? "—" : "choose…"}</option>
            <option value="true">true</option>
            <option value="false">false</option>
          </select>
          {problem}
        </>
      );
    }
    return <><input {...common} value={value} onChange={(e) => setCell(key, c.name, e.target.value)} />{problem}</>;
  };

  // Read-only cells show a raw FK id, which is useless to a human. Resolve it against the same
  // options map the edit dropdowns already loaded — no extra request. `#id` when unresolved.
  const displayText = (c: GridColumn, text: string): string => {
    if (showRaw || !c.references || text === "") return text;
    const hit = refs[c.references.table]?.options.find((o) => String(o.id) === text);
    return hit ? hit.label : `#${text}`;
  };

  if (error && !page) {
    return (
      <div className="grid-error">
        <p className="problems" role="alert">{error}</p>
        {loadFailed && <Button size="sm" onClick={() => setReload((n) => n + 1)}>Try again</Button>}
      </div>
    );
  }
  if (!page) return <div role="status"><span className="sr-only">Loading…</span><Skeleton rows={8} /></div>;

  const last = Math.min(offset + pageSize, page.total);
  const problemCount = Object.keys(problems).length;
  const hasFilters = Object.keys(v.filters).length > 0;
  const showEmpty = page.rows.length === 0 && draft.added.length === 0;
  return (
    <div className="grid-editor">
      <div className="grid-bar">
        <strong>{table}</strong>
        <span className="muted small">{page.total === 0 ? "no rows" : `${(offset + 1).toLocaleString()}–${last.toLocaleString()} of ${page.total.toLocaleString()}`}</span>
        <span className="spacer" />
        {canEdit && <Button size="sm" icon="plus" onClick={() => setDraft((d) => ({ ...d, added: [{}, ...d.added] }))}>Add row</Button>}
        {canEdit && <Button size="sm" icon="external" disabled={exporting} onClick={exportCsv}>{exporting ? "Exporting…" : "Export CSV"}</Button>}
        <label className="small">
          Rows per page{" "}
          <select aria-label="Rows per page" value={pageSize}
            onChange={(e) => { setPageSize(Number(e.target.value)); setOffset(0); }}>
            {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <label className="small" title="Resolve foreign keys to their labels, or show the stored ids">
          <input type="checkbox" checked={!showRaw} onChange={(e) => setShowRaw(!e.target.checked)} /> show labels
        </label>
        <label className="small">
          <input type="checkbox" checked={v.inactive} onChange={(e) => setView({ ...v, inactive: e.target.checked })} /> show inactive
        </label>
        <Button size="sm" variant="ghost" aria-pressed={showStats} onClick={() => setShowStats((x) => !x)}>Column stats</Button>
      </div>

      <FilterBar columns={columns.map((c) => c.name)} view={v} onChange={setView} />

      {error && (
        <div className="grid-error">
          <p className="problems" role="alert">{error}</p>
          {loadFailed && <Button size="sm" onClick={() => setReload((n) => n + 1)}>Try again</Button>}
        </div>
      )}
      {exportError && <p className="problems" role="alert">{exportError}</p>}
      {note && <p className="muted small" role="status">{note}</p>}
      {showStats && <ColumnStats columns={columns} rows={page.rows} />}

      <ProgressBar active={loading} label="Loading rows" />
      {showEmpty ? (
        hasFilters ? (
          <EmptyState icon="search" title="No rows match these filters."
            action={<Button onClick={() => setView({ ...v, filters: {} })}>Clear filters</Button>} />
        ) : (
          <EmptyState icon="table" title="No rows yet."
            action={canEdit ? <Button icon="plus" onClick={() => setDraft((d) => ({ ...d, added: [{}, ...d.added] }))}>Add row</Button> : undefined} />
        )
      ) : (
        <div className="grid-scroll" aria-busy={loading}>
          <table className="grid">
            <thead>
              <tr>
                {canEdit && <th className="grid-actions" aria-label="actions" />}
                {columns.map((c) => (
                  <th key={c.name} aria-sort={v.sort === c.name ? (v.dir === "asc" ? "ascending" : "descending") : "none"}>
                    <button className="link" onClick={() => sortBy(c.name)}>
                      {c.name}{v.sort === c.name ? (v.dir === "asc" ? " ▲" : " ▼") : ""}
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {draft.added.map((row, i) => (
                <tr key={`new${i}`} data-new="true">
                  <td className="grid-actions">
                    <button className="link" aria-label={`discard new row ${i + 1}`}
                      onClick={() => setDraft((d) => ({ ...d, added: d.added.filter((_, j) => j !== i) }))}>✕</button>
                    <span className="grid-new">new</span>
                  </td>
                  {columns.map((c) => (
                    <td key={c.name}>{c.readOnly ? <span className="muted small">auto</span> : input(c, `new${i}`, row[c.name] ?? "", true)}</td>
                  ))}
                </tr>
              ))}
              {page.rows.map((row) => {
                const id = row[page.primaryKey ?? "id"] as number;
                const removed = draft.removed.includes(id);
                return (
                  <tr key={id} data-removed={removed}>
                    {canEdit && (
                      <td className="grid-actions">
                        <button className="link" aria-label={`${removed ? "restore" : "remove"} row ${id}`} onClick={() => toggleRemove(id)}>
                          {removed ? "↺" : "✕"}
                        </button>
                      </td>
                    )}
                    {columns.map((c) => {
                      const edited = draft.edits[String(id)]?.[c.name] !== undefined;
                      const text = cellText(row, c.name, draft, page.primaryKey ?? "id");
                      return (
                        <td key={c.name} data-edited={edited}>
                          {!canEdit || c.readOnly || removed
                            ? <span title={displayText(c, text) !== text ? text : undefined}>{displayText(c, text)}</span>
                            : input(c, id, text, edited)}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="grid-pager">
        <Button size="sm" variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>Previous</Button>
        <span className="muted small">{page.total === 0 ? "" : `Page ${Math.floor(offset / pageSize) + 1} of ${Math.ceil(page.total / pageSize)}`}</span>
        <Button size="sm" variant="ghost" disabled={last >= page.total} onClick={() => setOffset(offset + pageSize)}>Next</Button>
      </div>

      {canEdit && pending > 0 && (
        <div className="grid-foot">
          <span className="small" data-testid="draft-count">
            {`${pending} pending ${pending === 1 ? "change" : "changes"}`}
            {problemCount > 0 && ` · ${problemCount} ${problemCount === 1 ? "cell needs" : "cells need"} fixing`}
          </span>
          <span className="spacer" />
          <Button variant="ghost" disabled={saving} onClick={() => { setDraft(emptyDraft()); setNote(""); }}>Discard</Button>
          <Button variant="primary" disabled={saving || problemCount > 0} onClick={save}>{saving ? "Saving…" : "Save for approval"}</Button>
        </div>
      )}
    </div>
  );
}
