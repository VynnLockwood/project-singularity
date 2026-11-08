"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  getGraph,
  getScript,
  listScripts,
  pasteScript,
  submitScript,
  uploadScript,
  validateScript,
  type GraphTable,
  type ScriptInfo,
  type ScriptReport,
} from "@/lib/api";

type Phase = "idle" | "uploaded" | "validated" | "submitted" | "error";

// ---------------------------------------------------------------- destination picker

const MAX_IDENT = 63;
const SNAKE = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/;

type Field = { source: string; target: string };
type Ref = { column: string; source: string; parent: string; parentKey: string };
type Entry = {
  source: string;                 // table as the script writes it
  mode: "new" | "existing";
  target: string;                 // create-new: the destination table name
  existing: string;               // existing: table of schema `master`
  fields: Field[];
  refs: Ref[];
};

const emptyEntry = (): Entry => ({
  source: "", mode: "new", target: "", existing: "", fields: [{ source: "", target: "" }], refs: [],
});

const destinationOf = (e: Entry) => (e.mode === "new" ? e.target : e.existing).trim();

const identProblem = (name: string) =>
  !SNAKE.test(name)
    ? "must be snake_case (lowercase letters, digits, single underscores, starting with a letter)"
    : name.length > MAX_IDENT
      ? `must be at most ${MAX_IDENT} characters`
      : "";

/** The JSON `POST /scripts/:id/validate` already takes: `columns` is destination -> source, and a ref
 * whose parent is not a table of the script carries `external: true`. Empty while anything is invalid,
 * so an incomplete picker never replaces the server's own suggestion. */
function emit(entries: Entry[]): string {
  const tables: Record<string, { target: string; columns: Record<string, string>; refs?: object[] }> = {};
  for (const e of entries) {
    const source = e.source.trim();
    const target = destinationOf(e);
    if (!source || !target) continue;
    const columns: Record<string, string> = {};
    for (const f of e.fields) {
      const s = f.source.trim();
      const t = f.target.trim() || (e.mode === "existing" ? "" : s);
      if (s && t) columns[t] = s;
    }
    const refs = e.refs
      .filter((r) => r.column.trim() && r.source.trim() && r.parent && r.parentKey)
      .map((r) => ({
        column: r.column.trim(),
        parent: `master.${r.parent}`,
        parent_key: r.parentKey,
        source_column: r.source.trim(),
        external: true,
      }));
    tables[source] = refs.length ? { target, columns, refs } : { target, columns };
  }
  return Object.keys(tables).length ? JSON.stringify({ tables }, null, 2) : "";
}

/** Readable reasons nothing will be sent — the same names the backend would reject. */
function problemsOf(entries: Entry[]): string[] {
  const out: string[] = [];
  const targets = new Map<string, number>();
  entries.forEach((e, i) => {
    const n = i + 1;
    const target = destinationOf(e);
    if (!e.source.trim() || !target) return;
    const badName = identProblem(target);
    if (badName) out.push(`Table ${n}: destination "${target}" ${badName}.`);
    const used = targets.get(target);
    if (used !== undefined) out.push(`Table ${n}: destination "${target}" is already used by table ${used}.`);
    else targets.set(target, n);
    const seenSource = new Set<string>();
    const seenTarget = new Set<string>();
    for (const f of e.fields) {
      const s = f.source.trim();
      const t = f.target.trim() || (e.mode === "existing" ? "" : s);
      if (!s || !t) continue;
      if (seenSource.has(s)) out.push(`Table ${n}: source column "${s}" is mapped more than once; map each source column once.`);
      seenSource.add(s);
      const badField = identProblem(t);
      if (badField) out.push(`Table ${n}: destination field "${t}" ${badField}.`);
      if (seenTarget.has(t)) out.push(`Table ${n}: destination field "${t}" is used twice.`);
      seenTarget.add(t);
    }
    e.refs.forEach((r, k) => {
      if (!r.column.trim() && !r.source.trim() && !r.parent && !r.parentKey) return; // row not started
      const column = r.column.trim();
      if (!column) out.push(`Table ${n} ref ${k + 1}: name the foreign key column (for example district_id).`);
      else if (seenTarget.has(column)) out.push(`Table ${n} ref ${k + 1}: "${column}" is both a destination field and a foreign key column.`);
      else {
        const badColumn = identProblem(column);
        if (badColumn) out.push(`Table ${n} ref ${k + 1}: foreign key column "${column}" ${badColumn}.`);
      }
      seenTarget.add(column);
      if (!r.source.trim()) out.push(`Table ${n} ref ${k + 1}: which source column holds the parent's key?`);
      if (!r.parent) out.push(`Table ${n} ref ${k + 1}: choose the existing table this ref points at.`);
      else if (!r.parentKey) out.push(`Table ${n} ref ${k + 1}: choose the column of master.${r.parent} it matches on.`);
    });
  });
  return out;
}

/** Pick where each table of the script lands and write the mapping JSON the backend already accepts. */
function DestinationPicker({ tables, onJson }: { tables: GraphTable[]; onJson: (json: string) => void }) {
  const [entries, setEntries] = useState<Entry[]>([emptyEntry()]);

  const patch = (i: number, part: Partial<Entry>) =>
    setEntries((prev) => prev.map((e, j) => (j === i ? { ...e, ...part } : e)));
  const patchField = (i: number, k: number, part: Partial<Field>) =>
    setEntries((prev) => prev.map((e, j) => (j === i
      ? { ...e, fields: e.fields.map((f, m) => (m === k ? { ...f, ...part } : f)) } : e)));
  const patchRef = (i: number, k: number, part: Partial<Ref>) =>
    setEntries((prev) => prev.map((e, j) => (j === i
      ? { ...e, refs: e.refs.map((r, m) => (m === k ? { ...r, ...part } : r)) } : e)));

  const issues = problemsOf(entries);
  const json = issues.length ? "" : emit(entries);
  useEffect(() => { onJson(json); }, [json, onJson]);

  const tableOptions = (value: string, onChange: (v: string) => void, label: (n: string) => string, blank: string, aria: string) => (
    <select aria-label={aria} value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{blank}</option>
      {tables.map((t) => <option key={t.name} value={t.name}>{label(t.name)}</option>)}
    </select>
  );

  return (
    <div>
      {entries.map((e, i) => {
        const n = i + 1;
        const dest = e.mode === "existing" ? tables.find((t) => t.name === e.existing) : undefined;
        return (
          <fieldset key={i}>
            <legend>Table {n} of the script</legend>

            <label>
              Source table in the script
              <input
                aria-label={`Table ${n} source table`}
                value={e.source}
                onChange={(ev) => patch(i, { source: ev.target.value })}
                placeholder="subdistrict_src"
              />
            </label>

            <fieldset>
              <legend>Destination</legend>
              <label>
                <input
                  type="radio"
                  name={`dest-mode-${i}`}
                  checked={e.mode === "new"}
                  onChange={() => patch(i, { mode: "new" })}
                  aria-label={`Table ${n}: create a new table`}
                />
                Create a new table
              </label>
              {e.mode === "new" && (
                <input
                  aria-label={`Table ${n} new table name`}
                  value={e.target}
                  onChange={(ev) => patch(i, { target: ev.target.value })}
                  placeholder="subdistrict"
                />
              )}
              <label>
                <input
                  type="radio"
                  name={`dest-mode-${i}`}
                  checked={e.mode === "existing"}
                  disabled={tables.length === 0}
                  onChange={() => patch(i, { mode: "existing" })}
                  aria-label={`Table ${n}: load into an existing table`}
                />
                Load into an existing table
              </label>
              {e.mode === "existing" && tableOptions(
                e.existing, (v) => patch(i, { existing: v }), (t) => `master.${t}`, "Choose a table…",
                `Table ${n} existing table`)}

              {e.fields.map((f, k) => (
                <div key={k}>
                  <input
                    aria-label={`Table ${n} field ${k + 1} source column`}
                    value={f.source}
                    onChange={(ev) => patchField(i, k, { source: ev.target.value })}
                    placeholder="source column"
                  />
                  {" → "}
                  {dest ? (
                    <select
                      aria-label={`Table ${n} field ${k + 1} destination field`}
                      value={f.target}
                      onChange={(ev) => patchField(i, k, { target: ev.target.value })}
                    >
                      <option value="">(not loaded)</option>
                      {dest.columns.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
                    </select>
                  ) : (
                    <input
                      aria-label={`Table ${n} field ${k + 1} destination field`}
                      value={f.target}
                      onChange={(ev) => patchField(i, k, { target: ev.target.value })}
                      placeholder={f.source.trim() || "destination field"}
                    />
                  )}
                  {e.fields.length > 1 && (
                    <button
                      aria-label={`Remove field ${k + 1} of table ${n}`}
                      onClick={() => patch(i, { fields: e.fields.filter((_, m) => m !== k) })}
                    >
                      ×
                    </button>
                  )}
                </div>
              ))}
              <button onClick={() => patch(i, { fields: [...e.fields, { source: "", target: "" }] })}>
                Add field to table {n}
              </button>

              {e.refs.map((r, k) => {
                const parent = tables.find((t) => t.name === r.parent);
                return (
                  <div key={k}>
                    <input
                      aria-label={`Table ${n} ref ${k + 1} foreign key column`}
                      value={r.column}
                      onChange={(ev) => patchRef(i, k, { column: ev.target.value })}
                      placeholder="district_id"
                    />
                    {" from source column "}
                    <input
                      aria-label={`Table ${n} ref ${k + 1} source column`}
                      value={r.source}
                      onChange={(ev) => patchRef(i, k, { source: ev.target.value })}
                      placeholder="district_code"
                    />
                    {" resolved against "}
                    {tableOptions(r.parent, (v) => patchRef(i, k, { parent: v, parentKey: "" }),
                      (t) => `master.${t}`, "Choose a parent table…", `Table ${n} ref ${k + 1} parent table`)}
                    {" matching its "}
                    <select
                      aria-label={`Table ${n} ref ${k + 1} parent key`}
                      value={r.parentKey}
                      disabled={!parent}
                      onChange={(ev) => patchRef(i, k, { parentKey: ev.target.value })}
                    >
                      <option value="">Choose a column…</option>
                      {(parent?.columns ?? []).map((c) => (
                        <option key={c.name} value={c.name}>{c.name}{c.primaryKey ? " (primary key)" : ""}</option>
                      ))}
                    </select>
                    <button
                      aria-label={`Remove ref ${k + 1} of table ${n}`}
                      onClick={() => patch(i, { refs: e.refs.filter((_, m) => m !== k) })}
                    >
                      ×
                    </button>
                  </div>
                );
              })}
              <button onClick={() => patch(i, { refs: [...e.refs, { column: "", source: "", parent: "", parentKey: "" }] })}>
                Add ref to table {n}
              </button>

              {entries.length > 1 && (
                <button onClick={() => setEntries((prev) => prev.filter((_, j) => j !== i))}>Remove table {n}</button>
              )}
            </fieldset>
          </fieldset>
        );
      })}
      <button onClick={() => setEntries((prev) => [...prev, emptyEntry()])}>Add another table</button>

      {issues.length > 0 && (
        <p className="problems" role="alert">{issues.join(" ")} Nothing is sent while these stand.</p>
      )}
      {issues.length === 0 && json && (
        <>
          <p className="muted">This mapping is sent with Validate:</p>
          <pre className="code" data-testid="mapping-json">{json}</pre>
        </>
      )}
      {issues.length === 0 && !json && (
        <p className="muted">Leave it empty and the server suggests a mapping instead.</p>
      )}
      {tables.length === 0 && (
        <p className="muted">No existing table could be loaded, so only "create a new table" is offered.</p>
      )}
    </div>
  );
}

/** Upload a pg_dump-style SQL file, validate it against the gate and submit it as one approvable change. */
export function ScriptsTab({ canEdit }: { canEdit: boolean }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [script, setScript] = useState<ScriptInfo | null>(null);
  const [report, setReport] = useState<ScriptReport | null>(null);
  const [mapping, setMapping] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pasteText, setPasteText] = useState("");
  const [tables, setTables] = useState<GraphTable[]>([]);

  const load = useCallback(() => listScripts().then((s) => setScript(s[0] ?? null)).catch(() => {}), []);
  useEffect(() => { if (canEdit) load(); }, [canEdit, load]);
  // existing tables: the destinations and ref parents the picker can offer (same list the ERD uses)
  useEffect(() => { if (canEdit) getGraph().then((g) => setTables(g.tables ?? [])).catch(() => {}); }, [canEdit]);

  const gotScript = (id: number, name: string, size: number) => {
    setScript({ id, name, size, status: "uploaded", change_id: null });
    setPhase("uploaded");
    setMapping("");
    setReport(null);
  };

  const upload = async (file: File) => {
    setBusy(true); setError(""); setPhase("idle"); setReport(null);
    try {
      const r = await uploadScript(file);
      gotScript(r.script_id, file.name, r.size);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Upload failed.");
      setPhase("error");
    } finally { setBusy(false); }
  };

  /** No local file? Paste the SQL text instead — same gate, same caps. */
  const paste = async () => {
    if (!pasteText.trim()) return;
    setBusy(true); setError(""); setPhase("idle"); setReport(null);
    try {
      const r = await pasteScript("pasted.sql", pasteText);
      gotScript(r.script_id, "pasted.sql", r.size);
      setPasteText("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Paste failed.");
      setPhase("error");
    } finally { setBusy(false); }
  };

  const doValidate = async () => {
    if (!script) return;
    setBusy(true); setError("");
    try {
      let data: unknown;
      if (mapping.trim()) {
        data = JSON.parse(mapping); // throws with a readable message on bad JSON
        if (typeof data !== "object" || data === null) throw new Error("mapping must be a JSON object");
      }
      // script_id must match the uploaded one: tell validate which script via the saved report
      const rep = await validateScript(script.id, data);
      setReport(rep);
      setPhase(rep.ok ? "validated" : "error");
      if (!rep.ok) setError("Validation found problems — fix the file and re-upload, or edit the mapping.");
      else setScript({ ...script, status: "validated", report: rep });
    } catch (e) {
      setError(e instanceof SyntaxError ? `Mapping is not valid JSON: ${e.message}` : e instanceof Error ? e.message : "Validation failed.");
      setPhase("error");
    } finally { setBusy(false); }
  };

  const doSubmit = async () => {
    if (!script || !report?.ok) return;
    setBusy(true); setError("");
    try {
      const r = await submitScript(script.id);
      setPhase("submitted");
      setScript({ ...script, status: "submitted", change_id: r.change_id });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Submit failed.");
      setPhase("error");
    } finally { setBusy(false); }
  };

  if (!canEdit) return <p className="muted">Viewers cannot load scripts. Ask an approver for the editor role.</p>;

  const orphanCount = (report?.tables ?? []).reduce((n, t) => n + t.orphans, 0);

  return (
    <div className="scripts">
      <h2>Scripts</h2>
      <p className="muted">
        Upload a pg_dump-style SQL file. It is parsed and checked by the same gate as every other change —
        nothing runs until an approver accepts it.
      </p>

      <label className="drop">
        <input
          type="file"
          accept=".sql,text/plain"
          disabled={busy}
          onChange={(e) => { const f = e.target.files?.[0]; if (f) void upload(f); }}
        />
        Choose a .sql file
      </label>

      {script && (
        <p>
          <code>{script.name}</code> · {script.size.toLocaleString()} bytes · status: {script.status}
          {script.change_id != null && <> · change <code>#{script.change_id}</code></>}
        </p>
      )}

      <label>…or paste the SQL here</label>
      <textarea
        rows={8}
        value={pasteText}
        onChange={(e) => setPasteText(e.target.value)}
        placeholder="-- pg_dump-style SQL, pasted"
        spellCheck={false}
        disabled={busy}
      />
      <div className="actions">
        <button onClick={() => void paste()} disabled={busy || !pasteText.trim()}>
          Upload pasted SQL
        </button>
      </div>

      <h3>Destination</h3>
      <DestinationPicker tables={tables} onJson={setMapping} />

      <details>
        <summary>Advanced: edit the mapping JSON</summary>
        <textarea
          rows={8}
          value={mapping}
          onChange={(e) => setMapping(e.target.value)}
          className="code"
          placeholder='{"tables": {"province": {"target": "province", "columns": {"code": "province_id"}}}}'
          spellCheck={false}
          aria-label="Mapping JSON (advanced)"
        />
        <p className="muted">Editing by hand wins until you change a field above — then the picker writes this again.</p>
      </details>

      <div className="actions">
        <button onClick={() => void doValidate()} disabled={busy || !script || phase === "submitted"}>
          Validate
        </button>
        <button onClick={() => void doSubmit()} disabled={busy || !report?.ok || phase === "submitted"}>
          Submit for approval
        </button>
      </div>

      {error && <p className="problems" role="alert">{error}</p>}

      {report && (
        <div className="report">
          <p>{report.ok ? "✔ ready to submit" : "✖ not ready"}</p>
          {report.dry_run && (
            <p>
              Dry run: {report.dry_run.ok ? "✔" : "✖"}
              {Object.entries(report.dry_run.rows_loaded ?? {}).map(([t, n]) => ` ${t}: ${n}`).join(", ")}
            </p>
          )}
          {orphanCount > 0 && <p className="problems">{orphanCount} orphan rows across {(report.tables ?? []).filter((t) => t.orphans > 0).length} table(s)</p>}
          {report.errors.map((e, i) => (
            <p key={i} className="problems">
              [{e.kind}] line {e.line}: {e.message}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
