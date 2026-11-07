"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  getScript,
  listScripts,
  pasteScript,
  submitScript,
  uploadScript,
  validateScript,
  type ScriptInfo,
  type ScriptReport,
} from "@/lib/api";

type Phase = "idle" | "uploaded" | "validated" | "submitted" | "error";

/** Upload a pg_dump-style SQL file, validate it against the gate and submit it as one approvable change. */
export function ScriptsTab({ canEdit }: { canEdit: boolean }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [script, setScript] = useState<ScriptInfo | null>(null);
  const [report, setReport] = useState<ScriptReport | null>(null);
  const [mapping, setMapping] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pasteText, setPasteText] = useState("");

  const load = useCallback(() => listScripts().then((s) => setScript(s[0] ?? null)).catch(() => {}), []);
  useEffect(() => { if (canEdit) load(); }, [canEdit, load]);

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

      <label>Mapping (optional JSON; leave empty to auto-suggest)</label>
      <textarea
        rows={5}
        value={mapping}
        onChange={(e) => setMapping(e.target.value)}
        placeholder='{"tables": {"province": {"pk": "province_id"}}, "columns": {...}}'
        spellCheck={false}
      />

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
