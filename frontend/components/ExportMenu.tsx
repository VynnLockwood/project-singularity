"use client";

import { useEffect, useRef, useState } from "react";
import { exportUrl } from "@/lib/api";

/** Export the selected schema as SQL. A preflight request surfaces server refusals before the browser download starts. */
export function ExportMenu({ schema, canExportData }: { schema: string; canExportData: boolean }) {
  const [withData, setWithData] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const ctl = useRef<AbortController | null>(null);

  useEffect(() => () => ctl.current?.abort(), []);
  useEffect(() => { setError(""); }, [schema, withData]);

  const run = async () => {
    const url = exportUrl(schema, canExportData && withData);
    const ac = new AbortController();
    ctl.current = ac;
    setBusy(true);
    setError("");
    try {
      const res = await fetch(url, { credentials: "same-origin", signal: ac.signal });
      if (!res.ok) {
        let msg = res.statusText || `Export failed (${res.status}).`;
        try {
          const body = await res.json();
          if (body && typeof body.error === "string" && body.error) msg = body.error;
        } catch { /* keep the status text */ }
        setError(msg);
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
      setBusy(false);
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
      <button className="button" onClick={run} disabled={busy}>Download SQL</button>
      {error && <p className="problems" role="alert">{error}</p>}
    </span>
  );
}
