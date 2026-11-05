"use client";

import { useState } from "react";
import type { Change, Me } from "@/lib/api";
import { canDecide, diffKind, warnings } from "@/lib/changes";

interface Props {
  change: Change;
  me: Me;
  onDecide?: (change: Change, action: "approve" | "reject") => Promise<void> | void;
  busy?: boolean;
}

/** One proposed change: diff (green added, red removed), warnings, collapsible SQL, Approve / Reject. */
export function ChangeCard({ change, me, onDecide, busy }: Props) {
  const [showSql, setShowSql] = useState(false);
  const warns = warnings(change);
  const blockers = (change.check.violations ?? []).filter((v) => v.severity === "block");
  const decidable = onDecide && canDecide(me, change);

  return (
    <section className="change" data-status={change.status} aria-label={`Change #${change.id}`}>
      <header className="change-head">
        <strong>
          #{change.id} · {change.kind === "schema" ? "Schema change" : "Data change"}
        </strong>
        <span className="badge" data-status={change.status}>
          {change.status}
        </span>
      </header>
      <p className="muted small">
        {change.requester} · {change.source}
        {change.prompt ? ` · “${change.prompt}”` : ""}
      </p>

      <ul className="diff">
        {change.diff.map((line, i) => (
          <li key={i} data-kind={diffKind(line)}>
            {line}
          </li>
        ))}
      </ul>

      {blockers.length > 0 && (
        <ul className="problems" aria-label="Blocked by rules">
          {blockers.map((v, i) => (
            <li key={i}>
              {v.message}
              {v.suggestion ? ` (try ${v.suggestion})` : ""}
            </li>
          ))}
        </ul>
      )}
      {warns.length > 0 && (
        <ul className="warnings" aria-label="Warnings">
          {warns.map((v, i) => (
            <li key={i}>⚠ {v.message}</li>
          ))}
        </ul>
      )}
      {change.error && <p className="problems">{change.error}</p>}
      {change.decisionNote && change.status === "rejected" && <p className="muted small">{change.decisionNote}</p>}

      {change.sql && change.sql.length > 0 && (
        <>
          <button className="link" onClick={() => setShowSql((s) => !s)} aria-expanded={showSql}>
            {showSql ? "Hide SQL" : "Show SQL"}
          </button>
          {showSql && <pre className="sql">{change.sql.join("\n")}</pre>}
        </>
      )}

      {decidable && (
        <div className="actions">
          <button className="button" disabled={busy} onClick={() => onDecide!(change, "approve")}>
            Approve
          </button>
          <button className="button ghost" disabled={busy} onClick={() => onDecide!(change, "reject")}>
            Reject
          </button>
        </div>
      )}
      {change.status === "pending" && !decidable && (
        <p className="muted small">Waiting for an approver.</p>
      )}
      {change.status === "applied" && change.decidedBy && (
        <p className="muted small">Applied · approved by {change.decidedBy}</p>
      )}
    </section>
  );
}
