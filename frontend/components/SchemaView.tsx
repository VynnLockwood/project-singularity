"use client";

import { useEffect, useMemo, useState } from "react";
import { getGraph, type SchemaGraph } from "@/lib/api";
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

export function SchemaView({ refreshKey }: { refreshKey: number }) {
  const [graph, setGraph] = useState<SchemaGraph | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    getGraph().then(setGraph).catch(() => setError("Could not load the schema."));
  }, [refreshKey]);
  if (error) return <p className="problems">{error}</p>;
  if (!graph) return <p className="muted">Loading…</p>;
  return <SchemaDiagram graph={graph} />;
}
