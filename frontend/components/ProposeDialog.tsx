"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, proposeChange } from "@/lib/api";
import type { Change, Me, Operation } from "@/lib/api";
import { describeOp } from "@/lib/databaseOps";
import { ChangeCard } from "./ChangeCard";

const MIN_REASON = 3;

interface Props {
  me: Me;
  schema: string;
  title: string;
  /** Called at render time with the trimmed reason ("" when not destructive). */
  build: (reason: string) => Operation[];
  destructive?: boolean;
  onClose: () => void;
  onProposed: (change: Change) => void;
}

/** Review the operations a UI action would send, then propose them. Refusals keep the dialog (and the reason) open. */
export function ProposeDialog({ me, schema, title, build, destructive, onClose, onProposed }: Props) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [change, setChange] = useState<Change | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  const trimmed = reason.trim();
  const reasonOk = !destructive || trimmed.length >= MIN_REASON;
  const ops = build(destructive ? trimmed : "");

  useEffect(() => {
    const first = dialogRef.current?.querySelector<HTMLElement>("input, textarea, button");
    (first ?? dialogRef.current)?.focus();
  }, []);

  const propose = async () => {
    if (busy || !reasonOk) return;
    setBusy(true);
    setError(null);
    try {
      const created = await proposeChange(ops, title, "web", schema);
      setChange(created);
      onProposed(created);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not reach the server. Try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="overlay">
      <div
        ref={dialogRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.stopPropagation();
            onClose();
          }
        }}
      >
        <h2 className="modal-title">{title}</h2>
        {change ? (
          <>
            <ChangeCard change={change} me={me} />
            <div className="modal-foot">
              <span />
              <button className="button" onClick={onClose}>
                Close
              </button>
            </div>
          </>
        ) : (
          <>
            <ul aria-label="Operations">
              {ops.map((op, i) => (
                <li key={i}>{describeOp(op)}</li>
              ))}
            </ul>
            {destructive && (
              <p>
                <label>
                  Reason
                  <input
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    aria-required="true"
                    disabled={busy}
                  />
                </label>
                {!reasonOk && <span className="muted small"> At least {MIN_REASON} characters.</span>}
              </p>
            )}
            {error && (
              <p className="problems" role="alert">
                {error}
              </p>
            )}
            <div className="modal-foot">
              <button className="link" onClick={onClose}>
                Close
              </button>
              <button className="button" onClick={propose} disabled={busy || !reasonOk || ops.length === 0}>
                {busy ? "Proposing…" : "Propose"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
