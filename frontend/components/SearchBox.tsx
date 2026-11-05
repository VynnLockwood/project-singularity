"use client";

import { useState } from "react";
import { ApiError, reindexSearch, searchSchema, type SearchResult } from "@/lib/api";

interface Props {
  /** Opens the chat with this text ready to send. */
  onAskAssistant: (text: string) => void;
  canPropose: boolean;
  /** Approvers can rebuild the whole index (needed after changing the embedding model). */
  canReindex?: boolean;
}

export function SearchBox({ onAskAssistant, canPropose, canReindex }: Props) {
  const [q, setQ] = useState("");
  const [asked, setAsked] = useState("");
  const [result, setResult] = useState<SearchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const run = async () => {
    const query = q.trim();
    if (!query || busy) return;
    setBusy(true);
    setError("");
    try {
      setResult(await searchSchema(query));
      setAsked(query);
    } catch (e) {
      setResult(null);
      setError(e instanceof ApiError ? e.message : "Search failed. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const rebuild = async () => {
    setBusy(true);
    setError("");
    setNote("");
    try {
      const r = await reindexSearch(true);
      setNote(`Index rebuilt: ${r.embedded} documents embedded.`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Rebuild failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="search" aria-label="Find where data belongs">
      <form onSubmit={(e) => { e.preventDefault(); run(); }} role="search">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Where does this data belong? e.g. customer phone number / เบอร์โทรลูกค้า"
          aria-label="Search the schema"
        />
        <button className="button" type="submit" disabled={busy || !q.trim()}>Search</button>
      </form>
      {canReindex && (
        <button className="link" onClick={rebuild} disabled={busy}>Rebuild search index</button>
      )}
      {note && <p className="muted small">{note}</p>}
      {error && <p className="problems" role="alert">{error}</p>}
      {result && (
        <>
          <ul className="hits" aria-label="Search results">
            {result.results.map((h) => (
              <li key={`${h.table}.${h.column ?? ""}`} data-confident={result.confident && h === result.results[0]}>
                <strong>{h.column ? `${h.table}.${h.column}` : h.table}</strong>
                <span className="muted small"> · {Math.round(h.confidence * 100)}%</span>
                <p className="muted small">{h.description}</p>
              </li>
            ))}
          </ul>
          {!result.confident && (
            <p>
              <span className="muted">{result.suggestion}</span>{" "}
              {canPropose && (
                <button className="link" onClick={() => onAskAssistant(`I need to store: ${asked}. Is there a place for it, or should we add a new column?`)}>
                  Ask the assistant to propose a new column
                </button>
              )}
            </p>
          )}
        </>
      )}
    </section>
  );
}
