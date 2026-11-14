"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, getSchemas, getTable, uploadScriptChunked, type Me, type SchemaInfo } from "@/lib/api";
import {
  answerImport, cancelImport, getImport, isRunning, recheckImport, startImport, submitImport,
  type ImportMessage, type ImportPlan, type ImportRun, type MaybeRow,
} from "@/lib/imports";
import { buildPath, type Route } from "@/lib/route";
import { Badge, Button, Panel, Skeleton } from "@/components/ui";

interface Props {
  me: Me;
  route: Extract<Route, { view: "import" }>;
  navigate: (route: Route) => void;
}

const ROLE: Record<ImportMessage["role"], string> = {
  profiler: "Profiler", matcher: "Matcher", designer: "Designer", critic: "Critic", person: "You", system: "System",
};
const POLL_MS = 1500;
const DROP = "__drop__";
const NEW = "__new__";

const message = (e: unknown, fallback: string) => (e instanceof ApiError && e.message ? e.message : e instanceof Error && e.message ? e.message : fallback);

export function ImportView({ route, navigate }: Props) {
  const [schemas, setSchemas] = useState<SchemaInfo[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    getSchemas().then((s) => alive && setSchemas(s), () => alive && setError("Could not load the databases."));
    return () => { alive = false; };
  }, []);
  const schema = route.schema ?? (schemas ? (schemas.find((s) => s.tables > 0) ?? schemas[0])?.name : undefined);

  if (route.run !== undefined) return <RunView key={route.run} id={route.run} schema={schema ?? "master"} navigate={navigate} />;
  if (error) return <p className="problems" role="alert">{error}</p>;
  if (!schemas || !schema) return <div role="status"><span className="sr-only">Loading…</span><Skeleton rows={6} /></div>;
  if (!schemas.some((s) => s.name === schema)) return <p className="problems" role="alert">There is no database called {schema}.</p>;
  return <StartForm schema={schema} schemas={schemas} navigate={navigate} />;
}

function StartForm({ schema, schemas, navigate }: { schema: string; schemas: SchemaInfo[]; navigate: Props["navigate"] }) {
  const [file, setFile] = useState<File | null>(null);
  const [prompt, setPrompt] = useState("");
  const [apiKey, setApiKey] = useState(""); // state only: never stored, sent on start and answer
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState("");
  const [error, setError] = useState("");
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  const start = async () => {
    if (!file) { setError("Choose a .xlsx or .csv file first."); return; }
    setBusy(true); setError(""); setStep("Uploading 0%");
    try {
      const up = await uploadScriptChunked(file, (done, total) => {
        if (alive.current) setStep(`Uploading ${Math.floor((done * 100) / Math.max(total, 1))}%`);
      }, undefined, "import");
      const r = await startImport({ uploadId: up.upload_id, schema, prompt, apiKey });
      if (alive.current) navigate({ view: "import", schema, run: r.id });
    } catch (e) {
      if (alive.current) setError(message(e, "Could not start the import."));
    } finally {
      if (alive.current) { setBusy(false); setStep(""); }
    }
  };

  return (
    <section className="imp" aria-labelledby="imp-title">
      <header className="imp-head"><h2 id="imp-title">Import</h2></header>
      <p className="imp-hint">Upload a spreadsheet. The assistant reads its shape, proposes where each column goes, and checks the result. Nothing is loaded until an approver accepts the change.</p>
      <div className="imp-form">
        <label className="imp-field">
          <span>Database</span>
          <select value={schema} onChange={(e) => navigate({ view: "import", schema: e.target.value })} disabled={busy}>
            {schemas.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
          </select>
        </label>
        <label className="imp-field">
          <span>File (.xlsx or .csv)</span>
          <input type="file" accept=".xlsx,.csv" disabled={busy} onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
        <label className="imp-field">
          <span>What should it do?</span>
          <textarea rows={3} value={prompt} maxLength={2000} disabled={busy} onChange={(e) => setPrompt(e.target.value)}
            placeholder="For example: these are vendors; load only the active ones." />
        </label>
        <label className="imp-field">
          <span>API key (only if not configured)</span>
          <input type="password" autoComplete="off" value={apiKey} disabled={busy} onChange={(e) => setApiKey(e.target.value)} />
        </label>
        <div className="imp-actions">
          <Button variant="primary" icon="inbox" onClick={() => void start()} disabled={busy}>Start</Button>
          {step && <span role="status" className="imp-step">{step}</span>}
        </div>
      </div>
      {error && <p className="problems" role="alert">{error}</p>}
    </section>
  );
}

type Cell = { kind: "col"; name: string } | { kind: "new"; name: string; type: string } | { kind: "drop" } | { kind: "ref"; label: string };
type Cells = Record<string, Record<string, Cell>>;
interface Edits { key: string; cells: Cells; skip: Record<string, number[]> }

const TYPES = ["text", "integer", "numeric", "date", "boolean"] as const;
const SNAKE = /^[a-z][a-z0-9_]{0,62}$/;
/** A snake_case column name made from a header ("Credit Limit" -> "credit_limit"). */
export function snakeName(header: string): string {
  const s = header.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 63);
  return !s ? "new_column" : /^[a-z]/.test(s) ? s : `c_${s}`.slice(0, 63);
}
const typeOf = (run: ImportRun, sheet: string, header: string) => {
  const t = run.profile?.sheets?.find((s) => s.name === sheet)?.columns?.find((c) => c.header === header)?.type;
  return (TYPES as readonly string[]).includes(t ?? "") ? (t as string) : "text";
};
/** Why a new column's name cannot be used, or "". */
const nameProblem = (name: string, taken: string[]) =>
  !SNAKE.test(name) ? "Use lowercase snake_case: letters, digits and _, starting with a letter."
  : taken.includes(name) ? `A column called ${name} already exists.` : "";

/** One cell per source header of every sheet in the plan. */
function initialCells(run: ImportRun): Cells {
  const out: Cells = {};
  const plan = run.plan;
  for (const [sheet, t] of Object.entries(plan?.mapping.tables ?? {})) {
    const headers = run.profile?.sheets?.find((s) => s.name === sheet)?.columns?.map((c) => c.header)
      ?? [...Object.values(t.columns ?? {}), ...(t.drop ?? []), ...(t.refs ?? []).map((r) => r.source_column ?? "")].filter(Boolean);
    const byHeader = new Map(Object.entries(t.columns ?? {}).map(([col, h]) => [h, col]));
    const added = new Map((t.add_columns ?? []).map((a) => [a.name, a.type]));
    const refs = new Map((t.refs ?? []).filter((r) => r.source_column).map((r) => [r.source_column as string, r]));
    out[sheet] = {};
    for (const h of headers) {
      const col = byHeader.get(h);
      const ref = refs.get(h);
      out[sheet][h] = col !== undefined && added.has(col) ? { kind: "new", name: col, type: added.get(col) as string }
        : col !== undefined ? { kind: "col", name: col }
        : ref ? { kind: "ref", label: `reference ${ref.column ?? ""} → ${ref.parent ?? ""}`.trim() }
        : { kind: "drop" };
    }
  }
  return out;
}

/** The plan's mapping with the person's edits applied. Refs, filters and everything else stay as the plan has them. */
function editedMapping(plan: ImportPlan, cells: Cells) {
  const tables: ImportPlan["mapping"]["tables"] = {};
  for (const [sheet, t] of Object.entries(plan.mapping.tables)) {
    const columns: Record<string, string> = {};
    const drop: string[] = [];
    const add_columns: { name: string; type: string; nullable: boolean }[] = [];
    for (const [header, c] of Object.entries(cells[sheet] ?? {})) {
      if (c.kind === "col") columns[c.name] = header;
      else if (c.kind === "new") { columns[c.name] = header; add_columns.push({ name: c.name, type: c.type, nullable: true }); }
      else if (c.kind === "drop") drop.push(header);
    }
    const { add_columns: _old, ...rest } = t;
    void _old;
    tables[sheet] = { ...rest, columns, drop, ...(add_columns.length ? { add_columns } : {}) };
  }
  return { ...plan.mapping, tables };
}

const lastProblems = (run: ImportRun): string[] => {
  if (run.report?.problems?.length) return run.report.problems;
  for (let i = run.messages.length - 1; i >= 0; i--) {
    const m = run.messages[i];
    const p = m.role === "critic" ? m.details?.problems : undefined;
    if (Array.isArray(p) && p.length) return p.map(String);
  }
  return [];
};

function RunView({ id, schema, navigate }: { id: number; schema: string; navigate: Props["navigate"] }) {
  const [run, setRun] = useState<ImportRun | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const [edits, setEdits] = useState<Edits | null>(null);
  const [answer, setAnswer] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [columns, setColumns] = useState<Record<string, string[]>>({});
  const alive = useRef(true);
  const seq = useRef(0); // the newest request: an older answer arriving later is dropped
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  /** Shows a run unless a newer request was made or the view is gone. */
  const accept = useCallback((s: number, r: ImportRun) => {
    if (!alive.current || s !== seq.current) return false;
    setRun(r);
    return true;
  }, []);

  // first load
  useEffect(() => {
    const s = ++seq.current;
    getImport(id).then((r) => { if (accept(s, r)) setError(""); }, (e) => {
      if (alive.current && s === seq.current) setError(message(e, "Could not load the import."));
    });
  }, [id, accept]);

  // poll while the server is working; each response schedules the next one
  const running = run !== null && isRunning(run.state);
  useEffect(() => {
    if (!running) return;
    const t = setTimeout(() => {
      const s = ++seq.current;
      getImport(id).then((r) => { if (accept(s, r)) setError(""); }, (e) => {
        if (!alive.current || s !== seq.current) return;
        setError(message(e, "Could not refresh the import."));
        if (!(e instanceof ApiError && e.status >= 400 && e.status < 500)) setTick((n) => n + 1); // a dropped connection: try again
      });
    }, POLL_MS);
    return () => clearTimeout(t);
  }, [running, run, tick, id, accept]);

  // the existing columns of each target table, for the selects
  const targets = useMemo(() => [...new Set(Object.values(run?.plan?.mapping.tables ?? {}).map((t) => t.target))], [run?.plan]);
  useEffect(() => {
    for (const t of targets) {
      getTable(t, schema).then((d) => { if (alive.current) setColumns((c) => ({ ...c, [t]: d.columns.map((x) => x.name) })); }, () => {});
    }
  }, [targets, schema]);

  const planKey = JSON.stringify(run?.plan ?? null);
  const dirty = edits?.key === planKey;
  const fresh = useMemo(() => ({
    cells: run ? initialCells(run) : {},
    skip: Object.fromEntries(Object.entries(run?.plan?.picked ?? {}).map(([k, v]) => [k, [...v]])),
  }), [run]);
  const cells = dirty ? edits.cells : fresh.cells;
  const skip = dirty ? edits.skip : fresh.skip;

  const setCell = (sheet: string, header: string, c: Cell) =>
    setEdits({ key: planKey, cells: { ...cells, [sheet]: { ...cells[sheet], [header]: c } }, skip });
  const toggleSkip = (sheet: string, row: number, on: boolean) => {
    const cur = skip[sheet] ?? [];
    setEdits({ key: planKey, cells, skip: { ...skip, [sheet]: on ? [...new Set([...cur, row])].sort((a, b) => a - b) : cur.filter((r) => r !== row) } });
  };

  /** Runs one action; its answer replaces the run unless something newer happened. */
  const act = async <T,>(fn: () => Promise<T>, after: (r: T, s: number) => void, fallback: string) => {
    setBusy(true); setError("");
    const s = ++seq.current;
    try {
      const r = await fn();
      if (alive.current) after(r, s);
    } catch (e) {
      if (alive.current) setError(message(e, fallback));
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  if (!run) {
    return error
      ? <p className="problems" role="alert">{error}</p>
      : <div role="status"><span className="sr-only">Loading…</span><Skeleton rows={6} /></div>;
  }

  const plan = run.plan;
  const state = run.state;
  const badNew = Object.entries(cells).some(([sheet, hs]) => {
    const taken = columns[plan?.mapping.tables[sheet]?.target ?? ""] ?? [];
    const names = Object.values(hs).flatMap((x) => (x.kind === "col" || x.kind === "new" ? [x.name] : []));
    return Object.values(hs).some((x) => x.kind === "new" && (nameProblem(x.name, taken) || names.filter((n) => n === x.name).length > 1));
  });
  const recheck = () => act(
    () => recheckImport(run.id, { mapping: editedMapping(plan as ImportPlan, cells), filters: plan?.filters ?? {}, skip }),
    (r, s) => { accept(s, r); setEdits(null); }, "Could not re-check.");
  const submit = async () => {
    setBusy(true); setError("");
    const s = ++seq.current;
    try {
      const r = await submitImport(run.id);
      if (alive.current && accept(s, { ...run, state: "submitted", changeId: r.changeId })) setEdits(null);
    } catch (e) {
      if (!alive.current) return;
      setError(message(e, "Could not submit."));
      // the server may have failed the run or changed its state: show what it says now
      if (e instanceof ApiError && (e.status === 400 || e.status === 409)) {
        const s2 = ++seq.current;
        await getImport(id).then((r) => { accept(s2, r); }, () => {});
      }
    } finally {
      if (alive.current) setBusy(false);
    }
  };
  const cancel = () => act(() => cancelImport(run.id), () => navigate({ view: "import", schema: run.schema }), "Could not cancel.");
  const send = () => act(
    () => answerImport(run.id, answer.trim(), apiKey),
    (r, s) => { accept(s, r); setAnswer(""); }, "Could not send the answer.");

  const problems = state === "failed" ? lastProblems(run) : [];
  const canEdit = state === "ready" || state === "failed";
  const changeLink = run.changeId != null ? buildPath({ view: "activity", tab: "history" }) : "";

  return (
    <section className="imp" aria-labelledby="imp-title">
      <header className="imp-head">
        <h2 id="imp-title">Import</h2>
        <span className="imp-file">{run.fileName} · {run.schema}</span>
        <Badge status={state === "ready" ? "applied" : state === "failed" ? "failed" : state === "submitted" ? "pending" : "stale"} />
        <span className="imp-state">{state.replace(/_/g, " ")}</span>
      </header>

      {error && <p className="problems" role="alert">{error}</p>}
      {state === "failed" && (
        <div className="problems" role="alert">
          <strong>The import could not be made to pass.</strong>
          {problems.length > 0 && <ul>{problems.map((p, i) => <li key={i}>{p}</li>)}</ul>}
        </div>
      )}

      <Panel title="Messages">
        <ol className="imp-thread">
          {run.messages.map((m, i) => (
            <li key={i} className="imp-msg" data-role={m.role} aria-label={`${ROLE[m.role] ?? m.role} message`}>
              <strong data-testid="role">{ROLE[m.role] ?? m.role}</strong>
              <p className="imp-text">{m.text}</p>
              {m.details && Object.keys(m.details).length > 0 && (
                <details><summary>Details</summary><pre>{JSON.stringify(m.details, null, 2)}</pre></details>
              )}
            </li>
          ))}
        </ol>
        {running && <p role="status" className="imp-step">Working…</p>}
      </Panel>

      {state === "waiting_for_answer" && (
        <Panel title="The assistant has a question">
          {(plan?.questions ?? []).length > 0 && <ol>{(plan?.questions ?? []).map((q, i) => <li key={i}>{q}</li>)}</ol>}
          <label className="imp-field">
            <span>Your answer</span>
            <textarea rows={3} maxLength={1000} value={answer} onChange={(e) => setAnswer(e.target.value)} />
          </label>
          <label className="imp-field">
            <span>API key (only if not configured)</span>
            <input type="password" autoComplete="off" value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
          </label>
          <div className="imp-actions">
            <Button variant="primary" onClick={() => void send()} disabled={busy || !answer.trim()}>Send answer</Button>
          </div>
        </Panel>
      )}

      {plan && Object.entries(plan.mapping.tables).map(([sheet, t]) => (
        <SheetPanel key={sheet} sheet={sheet} run={run} target={t.target} existing={columns[t.target]} cells={cells[sheet] ?? {}}
          editable={canEdit && !busy} skipped={skip[sheet] ?? []}
          onCell={(h, c) => setCell(sheet, h, c)} onSkip={(row, on) => toggleSkip(sheet, row, on)} />
      ))}

      {plan && <Counts run={run} />}

      {run.changeId != null && (
        <p className="imp-done" role="status">
          Submitted. <a href={changeLink} onClick={(e) => { e.preventDefault(); navigate({ view: "activity", tab: "history" }); }}>Change #{run.changeId}</a> waits for an approver.
        </p>
      )}

      <div className="imp-actions">
        <Button onClick={() => void recheck()} disabled={busy || !canEdit || !dirty || badNew} icon="refresh">Re-check</Button>
        <Button variant="primary" onClick={() => void submit()} disabled={busy || state !== "ready" || dirty || run.report?.ok === false}>Submit for approval</Button>
        <Button variant="danger" onClick={() => void cancel()} disabled={busy || running || state === "submitted"}>Cancel</Button>
      </div>
    </section>
  );
}

function SheetPanel({ sheet, run, target, existing, cells, editable, skipped, onCell, onSkip }: {
  sheet: string; run: ImportRun; target: string; existing: string[] | undefined; cells: Record<string, Cell>; editable: boolean;
  skipped: number[]; onCell: (header: string, c: Cell) => void; onSkip: (row: number, on: boolean) => void;
}) {
  const reasons = run.plan?.reasons ?? {};
  const problemFor = (name: string, header: string) => {
    const others = Object.entries(cells).filter(([h, x]) => h !== header && x.kind !== "drop" && x.kind !== "ref")
      .map(([, x]) => (x as { name: string }).name);
    return nameProblem(name, [...(existing ?? []), ...others]);
  };
  // The Matcher's full list, not the report's: the report leaves out rows already ticked, which could then never be unticked.
  const found = run.matches?.sources?.[sheet]?.rows;
  const listed: MaybeRow[] = found?.table === undefined || found.table === target ? found?.maybe ?? [] : [];
  const known = new Set(listed.map((m) => m.row));
  const maybe: MaybeRow[] = [...listed, ...skipped.filter((r) => !known.has(r)).map((row) => ({ row, id: 0, score: 0, existing: "" }))]
    .sort((a, b) => a.row - b.row);
  return (
    <Panel title={<>{sheet} → <code>{target}</code></>}>
      {reasons[sheet] && <p className="imp-reason">{reasons[sheet]}</p>}
      <table className="imp-map">
        <thead><tr><th scope="col">Source column</th><th scope="col">Goes to</th><th scope="col">Why</th></tr></thead>
        <tbody>
          {Object.entries(cells).map(([header, c]) => {
            const value = c.kind === "drop" ? DROP : c.kind === "new" ? NEW : c.kind === "col" ? c.name : "";
            const options = [...new Set([...(existing ?? []), ...(c.kind === "col" ? [c.name] : [])])];
            return (
              <tr key={header}>
                <th scope="row">{header}</th>
                <td>
                  {c.kind === "ref" ? <span>{c.label}</span> : (
                    <>
                      <select aria-label={`Target for ${header}`} value={value} disabled={!editable}
                        onChange={(e) => onCell(header, e.target.value === DROP ? { kind: "drop" }
                          : e.target.value === NEW ? { kind: "new", name: snakeName(header), type: typeOf(run, sheet, header) }
                          : { kind: "col", name: e.target.value })}>
                        {options.map((o) => <option key={o} value={o}>{o}</option>)}
                        {(existing !== undefined || c.kind === "new") && <option value={NEW}>new column</option>}
                        <option value={DROP}>drop</option>
                      </select>
                      {c.kind === "new" && (
                        <>
                          <input aria-label={`New column name for ${header}`} value={c.name} disabled={!editable} placeholder="column name"
                            onChange={(e) => onCell(header, { ...c, name: e.target.value })} />
                          <select aria-label={`Type of ${header}`} value={c.type} disabled={!editable}
                            onChange={(e) => onCell(header, { ...c, type: e.target.value })}>
                            {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                          </select>
                          {problemFor(c.name, header) && <span className="problems" role="note">{problemFor(c.name, header)}</span>}
                        </>
                      )}
                    </>
                  )}
                </td>
                <td className="imp-why">{reasons[`${sheet}.${header}`] ?? ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {maybe.length > 0 && (
        <fieldset className="imp-maybe">
          <legend>Maybe the same as an existing record. These are loaded unless you skip them.</legend>
          {maybe.map((m) => (
            <label key={m.row}>
              <input type="checkbox" aria-label={`Skip row ${m.row}`} checked={skipped.includes(m.row)} disabled={!editable}
                onChange={(e) => onSkip(m.row, e.target.checked)} />
              <span>{m.existing ? <>Row {m.row} looks like <em>{m.existing}</em> (match {Math.round(m.score * 100)}%)</> : <>Row {m.row}</>}</span>
            </label>
          ))}
        </fieldset>
      )}
    </Panel>
  );
}

function Counts({ run }: { run: ImportRun }) {
  const t = Object.values(run.report?.tables ?? {});
  if (!run.report) return null;
  const sum = (k: "new" | "exists" | "filtered" | "skipped") => t.reduce((n, x) => n + (x[k] ?? 0), 0);
  const items: [string, number][] = [
    ["New", sum("new")], ["Already exist (skipped)", sum("exists")], ["Removed by filter", sum("filtered")],
    ["Skipped by you", sum("skipped")], ["Bad rows", run.report.badRows?.length ?? 0], ["Dropped columns", run.report.dropped?.length ?? 0],
  ];
  return (
    <dl className="imp-counts" aria-label="Counts">
      {items.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
    </dl>
  );
}
