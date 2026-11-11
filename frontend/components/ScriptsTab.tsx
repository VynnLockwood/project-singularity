"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  getGraph,
  getSchemas,
  getScript,
  getScriptLimits,
  listScripts,
  pasteScript,
  submitScript,
  uploadScriptChunked,
  validateScriptAsync,
  type GraphTable,
  type ScriptInfo,
  type ScriptReport,
} from "@/lib/api";
import { explain } from "@/lib/scriptProblems";

const POLL_MS = 2000;       // how often the running check is asked for its phase
const DEBOUNCE_MS = 1000;   // a customize edit re-runs the check once typing stops

const PHASE_LABEL: Record<string, string> = {
  queued: "Starting", reading: "Reading", parsing: "Parsing", checking: "Checking rows", dry_run: "Dry run",
};
const KIND_LABEL: Record<string, string> = {
  logic: "Functions, triggers and policies", extension: "Extensions", sequence: "Sequences",
  default: "Sequence defaults", permission: "Permissions",
};
const SOURCE_LABEL: Record<string, string> = { detected: "named in the file", chosen: "your choice", default: "default" };
const KIND_ORDER = ["logic", "extension", "sequence", "default", "permission"];

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const fmtBytes = (n: number) => (n < 1024 ? `${n.toLocaleString()} bytes` : n < 2 ** 20
  ? `${(n / 1024).toFixed(1)} KB` : `${(n / 2 ** 20).toFixed(n % 2 ** 20 ? 1 : 0)} MB`);

/** The mapping JSON, or undefined for "let the server suggest one". Throws a readable SyntaxError/Error. */
function parseMapping(text: string): unknown {
  if (!text.trim()) return undefined;
  const data = JSON.parse(text);
  if (typeof data !== "object" || data === null || Array.isArray(data)) throw new Error("mapping must be a JSON object");
  return data;
}

// ---------------------------------------------------------------- destination picker

const MAX_IDENT = 63;
const SNAKE = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/;

type Field = { source: string; target: string };
type Ref = { column: string; source: string; parent: string; parentKey: string };
type Entry = {
  source: string;                 // table as the script writes it
  mode: "new" | "existing";
  target: string;                 // create-new: the destination table name
  existing: string;               // existing: table of the database the script is checked against
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
function emit(entries: Entry[], schema: string): string {
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
        parent: `${schema}.${r.parent}`,
        parent_key: r.parentKey,
        source_column: r.source.trim(),
        external: true,
      }));
    tables[source] = refs.length ? { target, columns, refs } : { target, columns };
  }
  return Object.keys(tables).length ? JSON.stringify({ tables }, null, 2) : "";
}

/** Readable reasons nothing will be sent — the same names the backend would reject. */
function problemsOf(entries: Entry[], schema: string): string[] {
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
      else if (!r.parentKey) out.push(`Table ${n} ref ${k + 1}: choose the column of ${schema}.${r.parent} it matches on.`);
    });
  });
  return out;
}

/** Pick where each table of the script lands and write the mapping JSON the backend already accepts. */
function DestinationPicker({ tables, schema, onJson }: { tables: GraphTable[]; schema: string; onJson: (json: string) => void }) {
  const [entries, setEntries] = useState<Entry[]>([emptyEntry()]);

  const patch = (i: number, part: Partial<Entry>) =>
    setEntries((prev) => prev.map((e, j) => (j === i ? { ...e, ...part } : e)));
  const patchField = (i: number, k: number, part: Partial<Field>) =>
    setEntries((prev) => prev.map((e, j) => (j === i
      ? { ...e, fields: e.fields.map((f, m) => (m === k ? { ...f, ...part } : f)) } : e)));
  const patchRef = (i: number, k: number, part: Partial<Ref>) =>
    setEntries((prev) => prev.map((e, j) => (j === i
      ? { ...e, refs: e.refs.map((r, m) => (m === k ? { ...r, ...part } : r)) } : e)));

  const issues = problemsOf(entries, schema);
  const json = issues.length ? "" : emit(entries, schema);
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
                onChange={(ev) => {
                  const v = ev.target.value;
                  // a source named like a table that already exists loads into it, instead of creating a second one
                  if (e.mode === "new" && !e.target.trim() && tables.some((t) => t.name === v.trim())) {
                    patch(i, { source: v, mode: "existing", existing: v.trim() });
                  } else patch(i, { source: v });
                }}
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
                e.existing, (v) => patch(i, { existing: v }), (t) => `${schema}.${t}`, "Choose a table…",
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
                      (t) => `${schema}.${t}`, "Choose a parent table…", `Table ${n} ref ${k + 1} parent table`)}
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

/** Choose a pg_dump-style SQL file: it is sent in parts, checked in the background and, when ready, submitted
 * as one approvable change. Nothing runs until an approver accepts it. */
export function ScriptsTab({ canEdit }: { canEdit: boolean }) {
  const [script, setScript] = useState<ScriptInfo | null>(null);
  const [report, setReport] = useState<ScriptReport | null>(null);
  const [mapping, setMapping] = useState("");
  const [error, setError] = useState("");
  const [retry, setRetry] = useState<null | "upload" | "check">(null);
  const [step, setStep] = useState("");
  const [busy, setBusy] = useState(false);
  const [pasteText, setPasteText] = useState("");
  const [tables, setTables] = useState<GraphTable[]>([]);
  const [schemas, setSchemas] = useState<string[]>([]);
  const [chosen, setChosen] = useState("");           // "" = detect from the file
  const [limits, setLimits] = useState<{ max_bytes?: number } | null>(null);

  const token = useRef(0);                         // a newer run cancels the polling of an older one
  const lastFile = useRef<File | null>(null);
  const lastMapped = useRef<string | null>(null);  // mapping text of the last run; null until one ran
  const mappingRef = useRef("");
  mappingRef.current = mapping;
  const chosenRef = useRef("");
  chosenRef.current = chosen;

  const dbSchema = report?.schema || chosen || "master";   // the tables offered are the ones of the database being checked
  useEffect(() => { if (canEdit) getGraph(dbSchema).then((g) => setTables(g.tables ?? [])).catch(() => {}); }, [canEdit, dbSchema]);
  useEffect(() => { if (canEdit) getSchemas().then((l) => setSchemas((l ?? []).map((s) => s.name))).catch(() => {}); }, [canEdit]);
  useEffect(() => { if (canEdit) getScriptLimits().then(setLimits).catch(() => {}); }, [canEdit]);
  useEffect(() => () => { token.current += 1; }, []);

  /** Follow a running check until it is done or failed (three failed polls in a row end it). */
  const follow = useCallback(async (id: number, mine: number) => {
    let misses = 0;
    for (;;) {
      await sleep(POLL_MS);
      if (mine !== token.current) return;
      let s: ScriptInfo;
      try { s = await getScript(id); misses = 0; } catch (e) {
        if (++misses >= 3) throw e;
        continue;
      }
      if (mine !== token.current) return;
      const rep = s.report;
      if (!rep || rep.state === "running") { setStep(PHASE_LABEL[rep?.phase ?? "queued"] ?? "Checking"); continue; }
      if (rep.state === "failed") {
        setError(rep.message || "The check failed. Try again."); setRetry("check"); setStep("");
        return;
      }
      setScript(s); setReport(rep); setStep(rep.ok ? "Ready" : "Needs changes");
      return;
    }
  }, []);

  /** Start the background check and follow it until it is done or failed. */
  const check = useCallback(async (id: number, mappingText: string, schema: string = chosenRef.current) => {
    const mine = ++token.current;
    setBusy(true); setError(""); setRetry(null); setReport(null); setStep("Starting");
    lastMapped.current = mappingText;
    try {
      await validateScriptAsync(id, parseMapping(mappingText), schema || undefined);
      await follow(id, mine);
    } catch (e) {
      if (mine !== token.current) return;
      setError(e instanceof SyntaxError ? `Mapping is not valid JSON: ${e.message}`
        : e instanceof Error ? e.message : "The check failed.");
      setRetry("check"); setStep("");
    } finally { if (mine === token.current) setBusy(false); }
  }, [follow]);

  // On load: show the latest script; a check still running is followed, a finished one is shown as it was
  const load = useCallback(() => listScripts().then(async (list) => {
    const latest = list[0] ?? null;
    setScript(latest);
    const rep = latest?.report;
    if (!latest || !rep) return;
    if (rep.state === "running") {
      const mine = ++token.current;
      setBusy(true); setStep(PHASE_LABEL[rep.phase ?? "queued"] ?? "Checking");
      try { await follow(latest.id, mine); } catch (e) {
        if (mine === token.current) { setError(e instanceof Error ? e.message : "The check failed."); setRetry("check"); setStep(""); }
      } finally { if (mine === token.current) setBusy(false); }
    } else if (rep.state === "done" || (rep.state === undefined && "ok" in rep)) {
      setReport(rep); setStep(rep.ok ? "Ready" : "Needs changes");
    } else if (rep.state === "failed") {
      setError(rep.message || "The check failed. Try again."); setRetry("check");
    }
  }).catch(() => {}), [follow]);
  useEffect(() => { if (canEdit) void load(); }, [canEdit, load]);

  const sendFile = async (file: File) => {
    lastFile.current = file;
    const max = limits?.max_bytes;
    if (file.size === 0) { setError("The file is empty."); setRetry(null); return; }
    if (typeof max === "number" && file.size > max) {
      setError(`This file is ${fmtBytes(file.size)}, larger than the ${fmtBytes(max)} limit. Split it into parts, or ask an approver to raise the limit.`);
      setRetry(null);
      return;
    }
    setBusy(true); setError(""); setRetry(null); setStep("Uploading 0%");
    let r: { script_id: number; size: number };
    try {
      r = await uploadScriptChunked(file, (done, total) => setStep(`Uploading ${Math.floor((done * 100) / Math.max(total, 1))}%`));
    } catch (e) {
      // the previous script stays as it was, and can still be validated
      setError(e instanceof Error ? e.message : "Upload failed.");
      setRetry("upload"); setStep(""); setBusy(false);
      return;
    }
    setScript({ id: r.script_id, name: file.name, size: r.size, status: "uploaded", change_id: null });
    setReport(null);
    await check(r.script_id, mappingRef.current);
  };

  /** No local file? Paste the SQL text instead — same gate, same caps. */
  const paste = async () => {
    if (!pasteText.trim()) return;
    setBusy(true); setError(""); setRetry(null); setStep("Uploading 0%");
    let r: { script_id: number; size: number };
    try {
      r = await pasteScript("pasted.sql", pasteText);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Paste failed."); setStep(""); setBusy(false);
      return;
    }
    setScript({ id: r.script_id, name: "pasted.sql", size: r.size, status: "uploaded", change_id: null });
    setReport(null); setPasteText("");
    await check(r.script_id, mappingRef.current);
  };

  const submitted = script?.status === "submitted";

  // Customize: one re-run after the mapping changes (and only after a run happened), never a loop
  useEffect(() => {
    if (!script || busy || submitted || lastMapped.current === null || mapping === lastMapped.current) return;
    try { parseMapping(mapping); } catch { return; }      // wait until it reads as JSON
    const t = setTimeout(() => void check(script.id, mapping), DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [mapping, script, busy, submitted, check]);

  const tryAgain = () => {
    if (retry === "upload" && lastFile.current) void sendFile(lastFile.current);
    else if (script) void check(script.id, mapping);
  };

  const useDatabase = (name: string) => {
    setChosen(name);
    chosenRef.current = name;
    if (script && !busy && !submitted) void check(script.id, mappingRef.current, name);
  };

  const doSubmit = async () => {
    if (!script || !report?.ok) return;
    setBusy(true); setError("");
    try {
      const r = await submitScript(script.id);
      setScript({ ...script, status: "submitted", change_id: r.change_id });
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : "Submit failed.");
    } finally { setBusy(false); }
  };

  if (!canEdit) return <p className="muted">Viewers cannot load scripts. Ask an approver for the editor role.</p>;

  const max = limits?.max_bytes;
  const tableCount = report?.ops_summary?.tables?.length ?? report?.tables?.length ?? 0;
  const rowCount = (report?.tables ?? []).reduce((n, t) => n + t.rows, 0);
  const orphanCount = (report?.tables ?? []).reduce((n, t) => n + t.orphans, 0);
  const skipped = report?.skipped ?? [];
  const groups = KIND_ORDER.map((kind) => ({ kind, items: skipped.filter((x) => x.kind === kind) })).filter((g) => g.items.length);
  const hasLogic = groups.some((g) => g.kind === "logic");

  return (
    <div className="scripts">
      <h2>Scripts</h2>
      <p className="muted">
        Choose a pg_dump-style SQL file. It is sent, checked by the same gate as every other change and shown
        here as ready or not — nothing runs until an approver accepts it.
      </p>

      <label className="drop">
        <input
          type="file"
          accept=".sql,text/plain"
          disabled={busy}
          onChange={(e) => { const f = e.target.files?.[0]; if (f) void sendFile(f); }}
        />
        Choose a .sql file
      </label>
      {typeof max === "number" && <p className="muted">Largest file: {fmtBytes(max)}.</p>}

      <label>
        Load into database{" "}
        <select aria-label="Database" value={chosen} disabled={busy} onChange={(e) => useDatabase(e.target.value)}>
          <option value="">Detect from the file</option>
          {schemas.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </label>

      {script && (
        <p>
          <code>{script.name}</code> · script <code>#{script.id}</code> · {script.size.toLocaleString()} bytes · status: {script.status}
          {script.change_id != null && <> · change <code>#{script.change_id}</code></>}
        </p>
      )}
      {step && <p role="status" className="step">{step}</p>}

      {error && (
        <p className="problems" role="alert">
          {error}{" "}
          {retry && <button onClick={tryAgain} disabled={busy}>Try again</button>}
        </p>
      )}

      {report?.schema && (
        <>
          <p className="muted">
            Checked against <code>{report.schema}</code> ({SOURCE_LABEL[report.schema_source ?? "default"]})
          </p>
          {report.detected_schema && report.detected_schema !== report.schema && (
            schemas.includes(report.detected_schema) ? (
              <p>
                The file names <code>{report.detected_schema}</code>.{" "}
                <button onClick={() => useDatabase(report.detected_schema!)} disabled={busy || submitted}>
                  Use {report.detected_schema} instead
                </button>
              </p>
            ) : (
              <p className="muted">The file names <code>{report.detected_schema}</code>, which is not a database here.</p>
            )
          )}
        </>
      )}

      {report?.ok && (
        <div className="report">
          <p className="ready">{`Ready: ${tableCount} ${tableCount === 1 ? "table" : "tables"}, ${rowCount.toLocaleString()} ${rowCount === 1 ? "row" : "rows"}`}</p>
          <button onClick={() => void doSubmit()} disabled={busy || submitted}>Submit for approval</button>
        </div>
      )}

      {report && !report.ok && (
        <div className="report">
          {report.errors.map((e, i) => {
            const x = explain(e);
            return (
              <div key={i} className="problems">
                <p>{x.text}</p>
                <p>{x.fix}</p>
                <details>
                  <summary>Details</summary>
                  <p>[{e.kind}] line {e.line}: {e.message}</p>
                  {e.statement && <pre className="code">{e.statement}</pre>}
                </details>
              </div>
            );
          })}
          {(report.errors_truncated ?? 0) > 0 && <p className="muted">…and {report.errors_truncated} more problems.</p>}
          {(report.gate?.errors ?? []).map((g, i) => <p key={`g${i}`} className="problems">{g}</p>)}
          {orphanCount > 0 && <p className="problems">{orphanCount} rows point at a parent row that does not exist, in {(report.tables ?? []).filter((t) => t.orphans > 0).length} table(s).</p>}
          {report.dry_run && !report.dry_run.skipped && !report.dry_run.ok && report.dry_run.error && (
            <p className="problems">The trial load failed: {report.dry_run.error}</p>
          )}
        </div>
      )}

      {report && groups.length > 0 && (
        <details open={hasLogic}>
          <summary>Not imported, and why ({skipped.length}{(report.skipped_truncated ?? 0) > 0 ? "+" : ""})</summary>
          {groups.map(({ kind, items }) => (
            <details key={kind} open={kind === "logic"} className={kind === "logic" ? "warning" : undefined}>
              <summary>{KIND_LABEL[kind]} ({items.length})</summary>
              {kind === "logic" && <p className="problems">Warning: this logic is not imported, so it will be missing after the import.</p>}
              <p className="muted">{items[0].reason}</p>
              <ul>{items.map((x, i) => <li key={i}>line {x.line}: <code>{x.statement}</code></li>)}</ul>
            </details>
          ))}
          {(report.skipped_truncated ?? 0) > 0 && <p className="muted">…and {report.skipped_truncated} more.</p>}
        </details>
      )}
      {report && (report.ignored ?? []).length > 0 && (
        <details>
          <summary>Notes ({report.ignored.length})</summary>
          <ul>{report.ignored.map((x, i) => <li key={i}>line {x.line}: <code>{x.statement}</code></li>)}</ul>
        </details>
      )}

      <details className="customize">
        <summary>Customize (optional)</summary>
        <h3>Destination</h3>
        <DestinationPicker tables={tables} schema={dbSchema} onJson={setMapping} />
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
          <p className="muted">Editing by hand wins until you change a field above — then the picker writes this again. The check runs again when you stop typing.</p>
        </details>
      </details>

      <div className="actions">
        <button onClick={() => script && void check(script.id, mapping)} disabled={busy || !script || submitted}>
          Validate
        </button>
      </div>

      <details>
        <summary>…or paste the SQL here</summary>
        <textarea
          rows={8}
          value={pasteText}
          onChange={(e) => setPasteText(e.target.value)}
          placeholder="-- pg_dump-style SQL, pasted"
          spellCheck={false}
          disabled={busy}
        />
        <div className="actions">
          <button onClick={() => void paste()} disabled={busy || !pasteText.trim()}>Upload pasted SQL</button>
        </div>
      </details>
    </div>
  );
}
