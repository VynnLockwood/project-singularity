"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError, getOptions, getRows, proposeChange,
  type Change, type GridColumn, type Me, type RefOptions, type RowsPage,
} from "@/lib/api";
import {
  buildOperations, cellText, draftSize, emptyDraft, validate, type Draft,
} from "@/lib/grid";

import { ProgressBar, Skeleton } from "@/components/ui";
import { ColumnStats } from "./ColumnStats";

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
}

export function GridEditor({ me, table, live, onProposed, onBusy }: Props) {
  const canEdit = me.role !== "viewer";
  const [page, setPage] = useState<RowsPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [sort, setSort] = useState<string | undefined>();
  const [dir, setDir] = useState<"asc" | "desc">("asc");
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [applied, setApplied] = useState<Record<string, string>>({});
  const [pageSize, setPageSize] = useState(50);
  const [inactive, setInactive] = useState(false);
  const [draft, setDraft] = useState<Draft>(emptyDraft());
  const [refs, setRefs] = useState<Record<string, RefOptions>>({});
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [reload, setReload] = useState(0);
  const [showStats, setShowStats] = useState(false);
  const [showRaw, setShowRaw] = useState(false);
  const [loading, setLoading] = useState(false);

  const appliedHere = live.filter((c) => c.status === "applied" && c.kind === "data" && c.tables.includes(table)).length;

  // Debounce filter typing.
  useEffect(() => {
    const t = setTimeout(() => { setApplied(filters); setOffset(0); }, 300);
    return () => clearTimeout(t);
  }, [filters]);

  // New table: start clean.
  useEffect(() => {
    setOffset(0); setSort(undefined); setDir("asc"); setFilters({}); setApplied({});
    setDraft(emptyDraft()); setNote(""); setError("");
  }, [table]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    onBusy?.(true);
    getRows(table, { offset, limit: pageSize, sort, dir, filters: applied, inactive })
      .then((p) => { if (!cancelled) { setPage(p); setError(""); } })
      .catch((e) => { if (!cancelled) setError(e instanceof ApiError ? e.message : "Could not load rows."); })
      .finally(() => { onBusy?.(false); if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [table, offset, pageSize, sort, dir, applied, inactive, reload, appliedHere]);

  // Load dropdown options for every reference column.
  useEffect(() => {
    if (!page) return;
    for (const c of page.columns) {
      const ref = c.references?.table;
      if (ref && !refs[ref]) getOptions(ref).then((o) => setRefs((r) => ({ ...r, [ref]: o }))).catch(() => {});
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

  const sortBy = (name: string) => {
    if (sort === name) setDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSort(name); setDir("asc"); }
    setOffset(0);
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
    if (ref) {
      // ponytail: labels toggle drives display text here too; option VALUES stay ids so editing still saves the FK
      const shown = displayText(c, value);
      return (
        <select {...common} value={value} title={bad || (shown !== value ? value : undefined)} onChange={(e) => setCell(key, c.name, e.target.value)}>
          <option value="">{c.nullable ? "—" : "choose…"}</option>
          {ref.options.map((o) => <option key={String(o.id)} value={String(o.id)}>{displayText(c, String(o.id))}</option>)}
          {value && !ref.options.some((o) => String(o.id) === value) && <option value={value}>{shown}</option>}
        </select>
      );
    }
    if (c.type === "boolean") {
      return (
        <select {...common} value={value} onChange={(e) => setCell(key, c.name, e.target.value)}>
          <option value="">{c.nullable ? "—" : "choose…"}</option>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      );
    }
    return <input {...common} value={value} onChange={(e) => setCell(key, c.name, e.target.value)} />;
  };

  // Read-only cells show a raw FK id, which is useless to a human. Resolve it against the same
  // options map the edit dropdowns already loaded — no extra request. `#id` when unresolved.
  const displayText = (c: GridColumn, text: string): string => {
    if (showRaw || !c.references || text === "") return text;
    const hit = refs[c.references.table]?.options.find((o) => String(o.id) === text);
    return hit ? hit.label : `#${text}`;
  };

  if (error && !page) return <p className="problems" role="alert">{error}</p>;
  if (!page) return <div role="status"><span className="sr-only">Loading…</span><Skeleton rows={8} /></div>;

  const last = Math.min(offset + pageSize, page.total);
  return (
    <div className="grid-editor">
      <div className="grid-bar">
        <strong>{table}</strong>
        <span className="muted small">{page.total === 0 ? "no rows" : `${offset + 1}–${last} of ${page.total}`}</span>
        <label className="small">
          <input type="checkbox" checked={inactive} onChange={(e) => { setInactive(e.target.checked); setOffset(0); }} /> show inactive
        </label>
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
        <button className="link" aria-pressed={showStats} onClick={() => setShowStats((v) => !v)}>Column stats</button>
        <span className="spacer" />
        <button className="link" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>Previous</button>
        <button className="link" disabled={last >= page.total} onClick={() => setOffset(offset + pageSize)}>Next</button>
        {canEdit && (
          <button className="button" onClick={() => setDraft((d) => ({ ...d, added: [{}, ...d.added] }))}>Add row</button>
        )}
      </div>

      {error && <p className="problems" role="alert">{error}</p>}
      {showStats && <ColumnStats columns={columns} rows={page.rows} />}

      <ProgressBar active={loading} label="Loading rows" />
      <div className="grid-scroll" aria-busy={loading}>
        <table className="grid">
          <thead>
            <tr>
              {canEdit && <th aria-label="actions" />}
              {columns.map((c) => (
                <th key={c.name} aria-sort={sort === c.name ? (dir === "asc" ? "ascending" : "descending") : "none"}>
                  <button className="link" onClick={() => sortBy(c.name)}>
                    {c.name}{sort === c.name ? (dir === "asc" ? " ▲" : " ▼") : ""}
                  </button>
                </th>
              ))}
            </tr>
            <tr>
              {canEdit && <th />}
              {columns.map((c) => (
                <th key={c.name}>
                  <input
                    className="filter" aria-label={`filter ${c.name}`} placeholder="filter"
                    value={filters[c.name] ?? ""} onChange={(e) => setFilters((f) => ({ ...f, [c.name]: e.target.value }))}
                  />
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {draft.added.map((row, i) => (
              <tr key={`new${i}`} data-new="true">
                <td>
                  <button className="link" aria-label={`discard new row ${i + 1}`}
                    onClick={() => setDraft((d) => ({ ...d, added: d.added.filter((_, j) => j !== i) }))}>✕</button>
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
                    <td>
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

      {canEdit && (
        <div className="grid-foot">
          <span className="muted small" data-testid="draft-count">
            {pending === 0 ? "No pending edits" : `${pending} pending ${pending === 1 ? "change" : "changes"}`}
            {Object.keys(problems).length > 0 && ` · ${Object.keys(problems).length} cell(s) need fixing`}
          </span>
          {note && <span className="muted small">{note}</span>}
          <span className="spacer" />
          <button className="link" disabled={pending === 0 || saving} onClick={() => { setDraft(emptyDraft()); setNote(""); }}>Discard</button>
          <button className="button" disabled={pending === 0 || saving || Object.keys(problems).length > 0} onClick={save}>
            {saving ? "Saving…" : "Save for approval"}
          </button>
        </div>
      )}
    </div>
  );
}
