"use client";

import { useEffect, useMemo, useState } from "react";
import { getGraph, type SchemaGraph } from "@/lib/api";
import { layoutUniverse, type Universe } from "@/lib/universe";
import { UniverseScene, webglAvailable } from "./UniverseScene";
import { CARD_W, HEAD_H, ROW_H, layoutSchema } from "@/lib/layout";
import { Button } from "./ui";

/** Pure drawing of a schema graph (no fetching, easy to test). Cards are buttons: choosing one highlights its relations. */
export function SchemaDiagram({ graph, selected = null, onSelect, fit = false }: {
  graph: SchemaGraph; selected?: string | null; onSelect?: (name: string | null) => void; fit?: boolean;
}) {
  const layout = useMemo(() => layoutSchema(graph), [graph]);
  const fks = useMemo(() => new Set(graph.relations.map((r) => `${r.from.table}.${r.from.column}`)), [graph]);
  if (graph.tables.length === 0) {
    return <p className="muted">No tables yet.</p>;
  }
  const toggle = (name: string) => onSelect?.(selected === name ? null : name);
  return (
    <div className={fit ? "schema-scroll schema-fit" : "schema-scroll"}>
      <svg
        width={layout.width}
        height={layout.height}
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        role="group"
        aria-label="Schema diagram"
        onKeyDown={(e) => { if (e.key === "Escape") onSelect?.(null); }}
      >
        {layout.edges.map((e, i) => {
          const mid = (e.x1 + e.x2) / 2;
          const touches = selected !== null && (e.relation.from.table === selected || e.relation.to.table === selected);
          return (
            <path
              key={i}
              data-testid="relation"
              data-hot={touches ? "true" : undefined}
              data-dim={selected !== null && !touches ? "true" : undefined}
              d={`M${e.x1},${e.y1} C${mid},${e.y1} ${mid},${e.y2} ${e.x2},${e.y2}`}
              className="edge"
            />
          );
        })}
        {layout.tables.map(({ table, x, y, h }) => (
          <g
            key={table.name}
            data-testid="table"
            className="erd-card"
            role="button"
            tabIndex={0}
            aria-pressed={selected === table.name}
            aria-label={table.name}
            transform={`translate(${x},${y})`}
            onClick={() => toggle(table.name)}
            onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(table.name); } }}
          >
            <rect width={CARD_W} height={h} rx={8} className="card" />
            <text x={12} y={22} className="card-title" aria-hidden="true">{table.name}</text>
            {table.columns.map((c, i) => (
              <text key={c.name} x={12} y={HEAD_H + i * ROW_H + 14} className="card-col" data-fk={fks.has(`${table.name}.${c.name}`) ? "true" : undefined} aria-hidden="true">
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

/** Side panel for the table picked in the diagram (2D or 3D). */
export function TablePanel({ graph, name, onOpenFields }: { graph: SchemaGraph; name: string; onOpenFields?: (name: string) => void }) {
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
      {onOpenFields && <Button size="sm" icon="table" onClick={() => onOpenFields(name)}>Open fields</Button>}
    </aside>
  );
}

type Mode = "3d" | "2d";

export function SchemaView({ schema, refreshKey, table = null, onOpenTable }: {
  schema: string; refreshKey: number; table?: string | null; onOpenTable?: (name: string) => void;
}) {
  const [graph, setGraph] = useState<SchemaGraph | null>(null);
  const [universe, setUniverse] = useState<Universe | null>(null);
  const [error, setError] = useState("");
  const [mode, setMode] = useState<Mode>(() => (webglAvailable() ? "3d" : "2d"));
  const [selected, setSelected] = useState<string | null>(table);
  const [fit, setFit] = useState(false);
  const [seenTable, setSeenTable] = useState(table);
  if (table !== seenTable) { // the left pane chose another table: the diagram follows
    setSeenTable(table);
    setSelected(table);
  }

  useEffect(() => {
    let current = true;
    setGraph(null);
    setUniverse(null);
    setError("");
    getGraph(schema)
      .then((g) => { if (current) setGraph(g); })
      .catch(() => { if (current) setError("Could not load the schema."); });
    return () => { current = false; };
  }, [schema, refreshKey]);

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
      <div className="grid-bar">
        <div className="segmented" role="group" aria-label="View">
          <button aria-pressed={mode === "2d"} onClick={() => setMode("2d")}>2D</button>
          {webglAvailable() && <button aria-pressed={mode === "3d"} onClick={() => setMode("3d")}>3D</button>}
        </div>
        {mode === "2d" && graph.tables.length > 0 && (
          <Button size="sm" variant="ghost" aria-pressed={fit} onClick={() => setFit((f) => !f)}>Fit to width</Button>
        )}
        {mode === "3d" && <span className="muted small">drag to orbit, scroll to zoom, click a table</span>}
      </div>
      {mode === "2d" || graph.tables.length === 0 ? (
        <div className="schema-2d">
          <SchemaDiagram graph={graph} selected={selected} onSelect={setSelected} fit={fit} />
          {selected && <TablePanel graph={graph} name={selected} onOpenFields={onOpenTable} />}
        </div>
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
            {selected && <TablePanel graph={graph} name={selected} onOpenFields={onOpenTable} />}
          </div>
        </div>
      )}
    </div>
  );
}
