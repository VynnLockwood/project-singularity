"use client";

import { useEffect, useState } from "react";
import { getGraph, type Change, type Me } from "@/lib/api";
import { GridEditor } from "./GridEditor";

interface Props {
  me: Me;
  live: Change[];
  refreshKey: number;
  onProposed?: (change: Change) => void;
  onBusy: (b: boolean) => void;
}

export function DataView({ me, live, refreshKey, onProposed, onBusy }: Props) {
  const [tables, setTables] = useState<string[] | null>(null);
  const [table, setTable] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    getGraph()
      .then((g) => {
        const names = g.tables.map((t) => t.name);
        setTables(names);
        setTable((cur) => (cur && names.includes(cur) ? cur : names[0] ?? ""));
      })
      .catch(() => setError("Could not load the tables."));
  }, [refreshKey]);

  if (error) return <p className="problems">{error}</p>;
  if (!tables) return <p className="muted">Loading…</p>;
  if (tables.length === 0) return <p className="muted">No tables yet. Ask the assistant to design one in the Chat tab.</p>;

  return (
    <div>
      <label className="small">
        Table{" "}
        <select aria-label="Table" value={table} onChange={(e) => setTable(e.target.value)}>
          {tables.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </label>
      {table && <GridEditor me={me} table={table} live={live} onProposed={onProposed} onBusy={onBusy} />}
    </div>
  );
}
