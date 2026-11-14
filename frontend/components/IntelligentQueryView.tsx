"use client";

import { Prec, type Extension } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, getSchemas, type Me, type SchemaInfo } from "@/lib/api";
import { ghostExtension, setGhost, type Accepted } from "@/lib/ghost";
import {
  commentsAbove, learnQuery, recallQuery, runQuery, sqlContextAbove, statementAt, suggestQuery,
  type FoundValue, type QueryResult, type Suggestion,
} from "@/lib/iquery";
import type { Route } from "@/lib/route";
import { Button, Kbd, Skeleton } from "@/components/ui";
import { SqlEditor } from "./SqlEditor";

interface Props {
  me: Me;
  route: Extract<Route, { view: "query" }>;
  navigate: (route: Route) => void;
}

/** What the gray text on screen came from, so accepting it can be learned later. */
interface Shown { requestId: string; comments: string[]; values: FoundValue[]; text: string }
/** The SQL the person accepted, as a range that follows their later edits. */
interface Taken { from: number; to: number; comments: string[]; values: FoundValue[] }

const storageKey = (schema: string) => `singularity.iquery.text.${schema}`;
const readText = (schema: string): string => {
  try { return window.localStorage.getItem(storageKey(schema)) ?? ""; } catch { return ""; }
};
const saveText = (schema: string, text: string) => {
  try { window.localStorage.setItem(storageKey(schema), text); } catch { /* storage unavailable: the text just won't persist */ }
};

const cell = (v: unknown) => (v === null || v === undefined ? <em>NULL</em> : typeof v === "object" ? JSON.stringify(v) : String(v));

function failure(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 429) return "Slow down a moment, then press Enter again.";
    if (e.status === 503) return "Suggestions are not set up yet: an approver needs to choose a chat model in Settings.";
    return e.message || "Could not get a suggestion.";
  }
  return "Could not get a suggestion.";
}

export function IntelligentQueryView({ route, navigate }: Props) {
  const [schemas, setSchemas] = useState<SchemaInfo[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    getSchemas().then(setSchemas).catch(() => setError("Could not load the databases."));
  }, []);
  const schema = route.schema ?? (schemas ? (schemas.find((s) => s.tables > 0) ?? schemas[0])?.name : undefined);

  if (error) return <p className="problems" role="alert">{error}</p>;
  if (!schemas || !schema) return <div role="status"><span className="sr-only">Loading…</span><Skeleton rows={6} /></div>;
  if (!schemas.some((s) => s.name === schema)) return <p className="problems" role="alert">There is no database called {schema}.</p>;
  return <Workspace key={schema} schema={schema} schemas={schemas} navigate={navigate} />;
}

function Workspace({ schema, schemas, navigate }: { schema: string; schemas: SchemaInfo[]; navigate: Props["navigate"] }) {
  const viewRef = useRef<EditorView | null>(null);
  const abort = useRef<AbortController | null>(null);
  const counter = useRef(0);
  const current = useRef("");           // the request whose answers may still be shown
  const shown = useRef<Shown | null>(null);
  const taken = useRef<Taken | null>(null);
  const takenFor = useRef("");          // the request already accepted: a later answer for it is dropped
  const [status, setStatus] = useState("");
  const [fromMemory, setFromMemory] = useState(false);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [runError, setRunError] = useState("");
  const [running, setRunning] = useState(false);
  const initial = useMemo(() => readText(schema), [schema]);

  const showAnswer = useCallback((view: EditorView, pos: number, comments: string[], r: Suggestion, id: string) => {
    if (!r.sql || current.current !== id || takenFor.current === id) return false;
    shown.current = { requestId: id, comments, values: r.values, text: r.sql };
    view.dispatch({ effects: setGhost.of({ pos, text: r.sql, requestId: id }) });
    return true;
  }, []);

  const onCommentEnter = useCallback((pos: number) => {
    const view = viewRef.current;
    if (!view) return;
    const text = view.state.doc.toString();
    const comments = commentsAbove(text, pos);
    if (!comments.length) return;
    abort.current?.abort();
    const ac = (abort.current = new AbortController());
    const id = `${Date.now().toString(36)}-${++counter.current}`;
    current.current = id;
    const body = { schema, comments, sqlContext: sqlContextAbove(text, pos), requestId: id };
    setStatus("Thinking…");
    setFromMemory(false);

    recallQuery(body, ac.signal).then((r) => {
      if (showAnswer(view, pos, comments, r, id)) { setFromMemory(true); setStatus(""); }
    }).catch(() => { /* the full answer reports problems; a missed memory hit is silent */ });

    suggestQuery(body, ac.signal).then((r) => {
      if (current.current !== id) return;
      const notes: string[] = [];
      if (r.searchOffline) notes.push("Table search is offline, so tables were matched by name.");
      if (r.lowConfidence) notes.push("Not sure which tables you mean.");
      if (showAnswer(view, pos, comments, r, id)) {
        setFromMemory(false);
        setStatus(notes.join(" "));
      } else if (!r.sql && takenFor.current !== id) {
        setStatus(shown.current?.requestId === id ? "" : [`No suggestion${r.reason ? `: ${r.reason}` : "."}`, ...notes].join(" "));
      }
    }).catch((e) => {
      if (current.current !== id || ac.signal.aborted) return;
      if (shown.current?.requestId !== id) setStatus(failure(e)); else setStatus("");
    });
  }, [schema, showAnswer]);

  const onAccept = useCallback((a: Accepted) => {
    const s = shown.current;
    takenFor.current = a.requestId;
    taken.current = s && s.requestId === a.requestId ? { from: a.from, to: a.to, comments: s.comments, values: s.values } : null;
    setFromMemory(false);
    setStatus("");
  }, []);

  const run = useCallback(async () => {
    const view = viewRef.current;
    if (!view) return;
    const sel = view.state.selection.main;
    const doc = view.state.doc.toString();
    let sql = "";
    let from = sel.from;
    let to = sel.to;
    if (!sel.empty) {
      sql = doc.slice(sel.from, sel.to).split("\n").filter((l) => !/^\s*--/.test(l)).join("\n").trim();
    } else {
      const st = statementAt(doc, sel.head);
      if (st) ({ from, to, sql } = st);
    }
    if (!sql) { setRunError("Put the cursor in a statement, or select the SQL to run."); setResult(null); return; }
    setRunning(true);
    setRunError("");
    try {
      const r = await runQuery(schema, sql);
      setResult(r);
      const t = taken.current;
      if (t && t.to > t.from && from < t.to && to > t.from) {
        taken.current = null;
        learnQuery({ schema, comments: t.comments, sql, values: t.values }).catch(() => { /* learning is a bonus */ });
      }
    } catch (e) {
      setResult(null);
      setRunError(e instanceof ApiError ? e.message : "The query could not be run.");
    } finally {
      setRunning(false);
    }
  }, [schema]);

  const live = useRef({ onCommentEnter, onAccept, run });
  live.current = { onCommentEnter, onAccept, run };
  const extensions = useMemo<Extension[]>(() => [
    ghostExtension({
      onCommentEnter: (p) => live.current.onCommentEnter(p),
      onAccept: (a) => live.current.onAccept(a),
      onDismiss: () => { setStatus(""); setFromMemory(false); },
    }),
    // the accepted range follows later edits
    EditorView.updateListener.of((u) => {
      const t = taken.current;
      if (t && u.docChanged) { t.from = u.changes.mapPos(t.from, 1); t.to = u.changes.mapPos(t.to, -1); }
    }),
    Prec.high(keymap.of([{ key: "Mod-Enter", run: () => { void live.current.run(); return true; } }])),
  ], []);

  useEffect(() => () => abort.current?.abort(), []);

  return (
    <section className="iq" aria-labelledby="iq-title">
      <header className="iq-head">
        <h2 id="iq-title">Intelligent query</h2>
        <label className="iq-pick">
          <span>Database</span>
          <select value={schema} onChange={(e) => navigate({ view: "query", schema: e.target.value })}>
            {schemas.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
          </select>
        </label>
        <span className="iq-spacer" />
        <Button variant="primary" icon="code" onClick={() => void run()} disabled={running}>Run</Button>
      </header>
      <p className="iq-hint">
        Write what you want as a <code>--</code> comment and press <Kbd>Enter</Kbd>. Press <Kbd>Tab</Kbd> to take the suggestion,
        <Kbd>Esc</Kbd> to drop it, <Kbd>Ctrl</Kbd> + <Kbd>Enter</Kbd> to run. Earlier comments count as context.
      </p>
      <SqlEditor
        key={schema}
        value={initial}
        onChange={(t) => saveText(schema, t)}
        extensions={extensions}
        onReady={(v) => { viewRef.current = v; }}
      />
      <div className="iq-status" role="status" aria-live="polite">
        {fromMemory && <span className="iq-source">From memory</span>}
        {status && <span>{status}</span>}
      </div>
      {runError && <p className="problems" role="alert">{runError}</p>}
      {result && (
        <div className="iq-result">
          <div className="iq-scroll">
            <table>
              <thead><tr>{result.columns.map((c, i) => <th key={i} scope="col">{c}</th>)}</tr></thead>
              <tbody>
                {result.rows.map((row, i) => <tr key={i}>{row.map((v, j) => <td key={j}>{cell(v)}</td>)}</tr>)}
              </tbody>
            </table>
          </div>
          <p className="iq-count">
            {result.rows.length} {result.rows.length === 1 ? "row" : "rows"}
            {result.truncated && " shown; the result was cut off, so narrow the query to see the rest."}
          </p>
        </div>
      )}
    </section>
  );
}
