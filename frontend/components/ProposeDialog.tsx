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

  const busyRef = useRef(false);
  busyRef.current = busy;
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busyRef.current) closeRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      mounted.current = false;
      document.removeEventListener("keydown", onKey);
    };
  }, []);

  useEffect(() => {
    if (change) dialogRef.current?.focus();
  }, [change]);

  useEffect(() => {
    const first = dialogRef.current?.querySelector<HTMLElement>("input, textarea, button");
    (first ?? dialogRef.current)?.focus();
  }, []);

  const propose = async () => {
    if (busyRef.current || !reasonOk || ops.length === 0) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    let created: Change;
    try {
      created = await proposeChange(ops, title, "web", schema);
    } catch (e) {
      busyRef.current = false;
      if (mounted.current) {
        setError(e instanceof ApiError ? e.message : "Could not reach the server. Try again.");
        setBusy(false);
      }
      return;
    }
    busyRef.current = false;
    if (mounted.current) {
      setChange(created);
      setBusy(false);
    }
    onProposed(created);
  };

  return (
    <div className="overlay">
      <div
        ref={dialogRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="propose-dialog-title"
        tabIndex={-1}
      >
        <h2 className="modal-title" id="propose-dialog-title">{title}</h2>
        {change ? (
          <div role="status">
            <ChangeCard change={change} me={me} />
            <div className="modal-foot">
              <span />
              <button className="button" onClick={onClose}>
                Close
              </button>
            </div>
          </div>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void propose();
            }}
          >
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
                    aria-describedby={reasonOk ? undefined : "propose-reason-hint"}
                    disabled={busy}
                  />
                </label>
                {!reasonOk && <span className="muted small" id="propose-reason-hint"> At least {MIN_REASON} characters.</span>}
              </p>
            )}
            {error && (
              <p className="problems" role="alert">
                {error}
              </p>
            )}
            <div className="modal-foot">
              <button type="button" className="link" onClick={onClose} disabled={busy}>
                Close
              </button>
              <button type="submit" className="button" disabled={busy || !reasonOk || ops.length === 0}>
                {busy ? "Proposing…" : "Propose"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
