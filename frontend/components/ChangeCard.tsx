"use client";

import { useState } from "react";
import type { Change, Me } from "@/lib/api";
import { canDecide, diffKind, warnings } from "@/lib/changes";
import { Badge, Button } from "@/components/ui";

interface Props {
  change: Change;
  me: Me;
  onDecide?: (change: Change, action: "approve" | "reject") => Promise<void> | void;
  /** A decision on this change is in flight: its two buttons wait. */
  deciding?: boolean;
  /** Older name for `deciding`. */
  busy?: boolean;
}

/** One proposed change: diff (green added, red removed), warnings, collapsible SQL, Approve / Reject. */
export function ChangeCard({ change, me, onDecide, deciding, busy }: Props) {
  const waiting = deciding || busy;
  const [showSql, setShowSql] = useState(false);
  const warns = warnings(change);
  const blockers = (change.check.violations ?? []).filter((v) => v.severity === "block");
  const decidable = onDecide && canDecide(me, change);
  const raw = change.kind === "sql";
  const reason = change.operations?.[0]?.reason || change.prompt;

  return (
    <section className="change" data-status={change.status} aria-label={`Change #${change.id}`}>
      <header className="change-head">
        <strong>
          #{change.id} · {raw ? "Raw SQL" : change.kind === "schema" ? "Schema change" : change.kind === "config" ? "Settings change" : "Data change"}
        </strong>
        <Badge status={change.status} className="badge" />
      </header>
      <p className="muted small">
        {change.requester} · {change.source}
        {change.prompt && !raw ? ` · “${change.prompt}”` : ""}
      </p>
      {raw && reason && <p><strong>Why:</strong> {reason}</p>}

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

      {raw && change.sql && change.sql.length > 0 && <pre className="sql">{change.sql.join(";\n")}</pre>}
      {raw && (
        <p className="muted small">
          Rules were not checked. This cannot be undone automatically; a backup of {change.schema ?? "master"} is taken before it runs.
        </p>
      )}
      {!raw && change.sql && change.sql.length > 0 && (
        <>
          <button className="link" onClick={() => setShowSql((s) => !s)} aria-expanded={showSql}>
            {showSql ? "Hide SQL" : "Show SQL"}
          </button>
          {showSql && <pre className="sql">{change.sql.join("\n")}</pre>}
        </>
      )}

      {decidable && (
        <div className="actions">
          <Button variant="primary" icon="check" disabled={waiting} onClick={() => onDecide!(change, "approve")}>
            {deciding ? "Applying…" : "Approve"}
          </Button>
          <Button variant="danger" disabled={waiting} onClick={() => onDecide!(change, "reject")}>
            Reject
          </Button>
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
