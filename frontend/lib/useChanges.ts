"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, decide as sendDecision, getQueue, type Change, type Me } from "./api";
import { canDecide, upsert } from "./changes";
import { connectEvents } from "./events";

export interface Changes {
  /** Newest known copy of every change seen in this session. */
  live: Change[];
  /** Pending changes this person can decide, oldest first. */
  mine: Change[];
  online: boolean;
  /** Id of the change a decision is in flight for. */
  deciding: number | null;
  error: string;
  /** Bumps when a schema change is applied, so schema views reload. */
  schemaKey: number;
  /** Record a change proposed from this browser. */
  add(change: Change): void;
  decide(change: Change, action: "approve" | "reject"): Promise<void>;
}

/** How busy the review queue is, from 0 (empty) to 1 (five or more waiting). Drives the background. */
export function activityLevel(pending: number): number {
  return Math.max(0, Math.min(1, pending / 5));
}

/** Owns the live list of changes: the first load, the event stream and decisions. */
export function useChanges(me: Me, onEvent?: (change: Change, prev: Change | undefined) => void): Changes {
  const [live, setLive] = useState<Change[]>([]);
  const [online, setOnline] = useState(false);
  const [deciding, setDeciding] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [schemaKey, setSchemaKey] = useState(0);
  const known = useRef(new Map<number, Change>());
  const listener = useRef(onEvent);
  listener.current = onEvent;

  const record = useCallback((change: Change) => {
    const prev = known.current.get(change.id);
    known.current.set(change.id, change);
    setLive((l) => upsert(l, change));
    if (change.status === "applied" && change.kind === "schema" && prev?.status !== "applied") setSchemaKey((k) => k + 1);
    return prev;
  }, []);

  useEffect(() => {
    let current = true;
    getQueue().then((q) => { if (current && Array.isArray(q)) q.forEach(record); }).catch(() => {});
    const stop = connectEvents((e) => { const prev = record(e.change); listener.current?.(e.change, prev); }, setOnline);
    return () => { current = false; stop(); };
  }, [record]);

  const decide = useCallback(async (change: Change, action: "approve" | "reject") => {
    setDeciding(change.id);
    setError("");
    try {
      record({ ...change, ...(await sendDecision(change.id, action)) });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not record the decision.");
    } finally {
      setDeciding(null);
    }
  }, [record]);

  const add = useCallback((change: Change) => { record(change); }, [record]);
  const mine = useMemo(() => live.filter((c) => canDecide(me, c)), [live, me]);

  return { live, mine, online, deciding, error, schemaKey, add, decide };
}
