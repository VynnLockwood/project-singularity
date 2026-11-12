"use client";

import { useEffect, useRef, useState } from "react";
import { exportUrl, type ExportFormat } from "@/lib/api";

/** Export the selected schema as SQL. A preflight request surfaces server refusals before the browser download starts. */
export function ExportMenu({ schema, canExportData }: { schema: string; canExportData: boolean }) {
  const [withData, setWithData] = useState(false);
  const [format, setFormat] = useState<ExportFormat>("insert");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const ctl = useRef<AbortController | null>(null);

  useEffect(() => () => ctl.current?.abort(), []);
  useEffect(() => {
    setError("");
    ctl.current?.abort(); // a late result must not touch the new selection
  }, [schema, withData, format]);

  const run = async () => {
    const url = exportUrl(schema, canExportData && withData, format);
    const ac = new AbortController();
    ctl.current = ac;
    setBusy(true);
    setError("");
    try {
      const res = await fetch(url, { credentials: "same-origin", signal: ac.signal });
      if (ac.signal.aborted) return;
      if (!res.ok) {
        let msg = res.statusText || `Export failed (${res.status}).`;
        try {
          const body = await res.json();
          if (body && typeof body.error === "string" && body.error) msg = body.error;
        } catch { /* keep the status text */ }
        if (!ac.signal.aborted) setError(msg);
        return;
      }
      ac.abort(); // headers are enough; the real download is a separate request
      const a = document.createElement("a");
      a.href = url;
      a.download = "";
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch {
      if (!ac.signal.aborted) setError("Could not reach the server.");
    } finally {
      if (ctl.current === ac) setBusy(false);
    }
  };

  const hint = canExportData ? undefined : "Editors can export data";
  return (
    <span role="group" aria-label="Export SQL">
      <label className="small" title={hint}>
        <input
          type="checkbox"
          aria-label="Include data"
          title={hint}
          checked={canExportData && withData}
          disabled={!canExportData}
          onChange={(e) => setWithData(e.target.checked)}
        />{" "}
        Include data
      </label>{" "}
      {canExportData && withData && (
        <>
          <label className="small">
            Format{" "}
            <select aria-label="Data format" value={format} onChange={(e) => setFormat(e.target.value as ExportFormat)}>
              <option value="insert">Any SQL client</option>
              <option value="copy">psql only (faster)</option>
            </select>
          </label>{" "}
        </>
      )}
      <button className="button" onClick={run} disabled={busy}>Download SQL</button>
      {canExportData && withData && (
        <div className="small" role="note">
          {format === "insert"
            ? "Runs in pgAdmin, DBeaver or psql. The whole file is one transaction, so if it stops nothing is kept. Restore into an empty database."
            : "Only psql can run this file: psql -v ON_ERROR_STOP=1 -f file.sql. SQL editors will fail on it."}
        </div>
      )}
      {error && <div className="problems" role="alert">{error}</div>}
    </span>
  );
}
