"use client";

import { useEffect, useMemo, useState } from "react";
import { getGraph, type SchemaGraph } from "@/lib/api";
import { layoutUniverse, type Universe } from "@/lib/universe";
import { UniverseScene, webglAvailable } from "./UniverseScene";
import { CARD_W, HEAD_H, ROW_H, layoutSchema } from "@/lib/layout";

/** Pure drawing of a schema graph (no fetching, easy to test). */
export function SchemaDiagram({ graph }: { graph: SchemaGraph }) {
  const layout = useMemo(() => layoutSchema(graph), [graph]);
  if (graph.tables.length === 0) {
    return <p className="muted">No tables yet. Ask the assistant to design one in the Chat tab.</p>;
  }
  return (
    <div className="schema-scroll">
      <svg width={layout.width} height={layout.height} role="img" aria-label="Schema diagram">
        {layout.edges.map((e, i) => {
          const mid = (e.x1 + e.x2) / 2;
          return (
            <path
              key={i}
              data-testid="relation"
              d={`M${e.x1},${e.y1} C${mid},${e.y1} ${mid},${e.y2} ${e.x2},${e.y2}`}
              className="edge"
            />
          );
        })}
        {layout.tables.map(({ table, x, y, h }) => (
          <g key={table.name} data-testid="table" transform={`translate(${x},${y})`}>
            <rect width={CARD_W} height={h} rx={8} className="card" />
            <text x={12} y={22} className="card-title">{table.name}</text>
            {table.columns.map((c, i) => (
              <text key={c.name} x={12} y={HEAD_H + i * ROW_H + 14} className="card-col">
                {c.primaryKey ? "🔑 " : ""}{c.name}
                <tspan x={CARD_W - 10} textAnchor="end" className="card-type">{c.type}</tspan>
              </text>
            ))}
          </g>
        ))}
      </svg>
    </div>
  );
}

/** Side panel for the table picked in the 3D view. */
export function TablePanel({ graph, name }: { graph: SchemaGraph; name: string }) {
  const t = graph.tables.find((x) => x.name === name);
  if (!t) return null;
  const out = graph.relations.filter((r) => r.from.table === name);
  const into = graph.relations.filter((r) => r.to.table === name);
  return (
    <aside className="universe-panel" aria-label={`Table ${name}`}>
      <h3>{t.name}</h3>
      {t.comment && <p className="muted small">{t.comment}</p>}
      <p className="muted small">about {t.rowEstimate} rows</p>
      <ul className="small">
        {t.columns.map((c) => <li key={c.name}>{c.primaryKey ? "🔑 " : ""}{c.name} <span className="muted">{c.type}</span></li>)}
      </ul>
      {out.length > 0 && <p className="small">Points to: {out.map((r) => `${r.to.table} (via ${r.from.column})`).join(", ")}</p>}
      {into.length > 0 && <p className="small">Referenced by: {into.map((r) => `${r.from.table}.${r.from.column}`).join(", ")}</p>}
    </aside>
  );
}

type Mode = "3d" | "2d";

export function SchemaView({ refreshKey }: { refreshKey: number }) {
  const [graph, setGraph] = useState<SchemaGraph | null>(null);
  const [universe, setUniverse] = useState<Universe | null>(null);
  const [error, setError] = useState("");
  const [mode, setMode] = useState<Mode>(() => (webglAvailable() ? "3d" : "2d"));
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    getGraph().then(setGraph).catch(() => setError("Could not load the schema."));
  }, [refreshKey]);

  useEffect(() => {
    if (!graph) return;
    let live = true;
    layoutUniverse(graph).then((u) => { if (live) setUniverse(u); });
    return () => { live = false; };
  }, [graph]);

  useEffect(() => {
    if (graph && selected && !graph.tables.some((t) => t.name === selected)) setSelected(null);
  }, [graph, selected]);

  if (error) return <p className="problems">{error}</p>;
  if (!graph) return <p className="muted">Loading…</p>;

  return (
    <div>
      <div className="grid-bar" role="group" aria-label="View">
        <button className="link" aria-pressed={mode === "3d"} onClick={() => setMode("3d")}>3D</button>
        <button className="link" aria-pressed={mode === "2d"} onClick={() => setMode("2d")}>2D</button>
        {mode === "3d" && <span className="muted small">drag to orbit, scroll to zoom, click a table</span>}
      </div>
      {mode === "2d" || graph.tables.length === 0 ? (
        <SchemaDiagram graph={graph} />
      ) : (
        <div className="universe-wrap">
          {universe ? (
            <UniverseScene universe={universe} selected={selected} onSelect={setSelected} onUnavailable={() => setMode("2d")} />
          ) : (
            <p className="muted">Laying out…</p>
          )}
          <div>
            <ul className="universe-list" aria-label="Tables">
              {graph.tables.map((t) => (
                <li key={t.name}>
                  <button className="link" aria-pressed={selected === t.name} onClick={() => setSelected(selected === t.name ? null : t.name)}>{t.name}</button>
                </li>
              ))}
            </ul>
            {selected && <TablePanel graph={graph} name={selected} />}
          </div>
        </div>
      )}
    </div>
  );
}
