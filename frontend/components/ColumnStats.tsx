"use client";

import { useEffect, useState } from "react";
import type { GridColumn, Row } from "@/lib/api";
import { computeStats, type ColumnStats as Stats } from "@/lib/stats";

/** Quick statistics for each column of the rows currently on screen (a sample, not the whole table). */
export function ColumnStats({ columns, rows }: { columns: GridColumn[]; rows: Row[] }) {
  const [out, setOut] = useState<Record<string, Stats>>({});
  const [engine, setEngine] = useState<"wasm" | "js">("js");

  useEffect(() => {
    let cancelled = false;
    Promise.all(columns.map(async (c) => [c.name, await computeStats(rows.map((r) => r[c.name] ?? null))] as const)).then((all) => {
      if (cancelled) return;
      setOut(Object.fromEntries(all.map(([n, r]) => [n, r.stats])));
      if (all[0]) setEngine(all[0][1].engine);
    });
    return () => { cancelled = true; };
  }, [columns, rows]);

  return (
    <section className="stats" aria-label="Column statistics">
      <p className="muted small">Based on the {rows.length} rows shown · computed with {engine === "wasm" ? "WebAssembly" : "JavaScript"}</p>
      <div className="stat-cards">
        {columns.filter((c) => out[c.name]).map((c) => {
          const s = out[c.name];
          return (
            <div key={c.name} className="stat-card" data-testid={`stats-${c.name}`}>
              <strong>{c.name}</strong>
              <p className="small">{s.distinct} distinct · {s.nullPct}% empty</p>
              {s.numeric && <p className="small">min {s.numeric.min} · max {s.numeric.max} · mean {s.numeric.mean}</p>}
              {s.text && <p className="small">length {s.text.minLength}–{s.text.maxLength}</p>}
              <p className="muted small">{s.top.map((t) => `${t.value || "∅"} ×${t.count}`).join(", ")}</p>
            </div>
          );
        })}
      </div>
    </section>
  );
}
