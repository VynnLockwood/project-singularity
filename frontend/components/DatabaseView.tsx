"use client";

import { useEffect, useRef, useState } from "react";
import { getGraph, getSchemas, type Change, type Me, type SchemaGraph, type SchemaInfo } from "@/lib/api";
import { SchemaView } from "./SchemaView";

export type DbTab = "fields" | "relations" | "erd" | "overview";

const TABS: { id: DbTab; label: string }[] = [
  { id: "fields", label: "Fields" },
  { id: "relations", label: "Relations" },
  { id: "erd", label: "ERD" },
  { id: "overview", label: "Overview" },
];

export function DatabaseView({ me, refreshKey }: {
  me: Me; live: Change[]; refreshKey: number;
  onProposed: (c: Change) => void; onAskAssistant: (text: string) => void;
}) {
  void me;
  const [schemas, setSchemas] = useState<SchemaInfo[] | null>(null);
  const [schemasError, setSchemasError] = useState("");
  const [schema, setSchema] = useState("master");
  const [graph, setGraph] = useState<SchemaGraph | null>(null);
  const [graphError, setGraphError] = useState("");
  const [table, setTable] = useState<string | null>(null);
  const [tab, setTab] = useState<DbTab>("overview");
  const [filter, setFilter] = useState("");
  const seq = useRef(0);

  useEffect(() => {
    let current = true;
    getSchemas()
      .then((list) => {
        if (!current) return;
        setSchemas(list);
        setSchemasError("");
        setSchema((s) => (list.some((x) => x.name === s) ? s : "master"));
      })
      .catch(() => { if (current) setSchemasError("Could not load the databases."); });
    return () => { current = false; };
  }, [refreshKey]);

  useEffect(() => {
    const id = ++seq.current;
    setGraphError("");
    getGraph(schema)
      .then((g) => { if (id === seq.current) setGraph(g); })
      .catch(() => { if (id === seq.current) setGraphError("Could not load the tables."); });
  }, [schema, refreshKey]);

  useEffect(() => {
    if (graph && table && !graph.tables.some((t) => t.name === table)) setTable(null);
  }, [graph, table]);

  const pickSchema = (name: string) => {
    if (name === schema) return;
    seq.current++;
    setGraph(null);
    setTable(null);
    setTab("overview");
    setFilter("");
    setSchema(name);
  };

  const pickTable = (name: string | null) => {
    setTable(name);
    setTab(name ? "fields" : "overview");
  };

  const shown = (graph?.tables ?? []).filter((t) => t.name.toLowerCase().includes(filter.trim().toLowerCase()));

  return (
    <div className="db-layout">
      <nav className="db-rail" aria-label="Databases">
        {schemasError && <p className="problems">{schemasError}</p>}
        {schemas === null && !schemasError && <p className="muted">Loading…</p>}
        <ul className="db-list">
          {(schemas ?? []).map((s) => (
            <li key={s.name}>
              <button className="link" aria-current={s.name === schema ? "true" : undefined} onClick={() => pickSchema(s.name)}>
                {`${s.name} (${s.tables})`}
              </button>
            </li>
          ))}
        </ul>
        <div className="db-rail-actions" />
        <h3>Tables</h3>
        <input aria-label="Filter tables" placeholder="Filter tables" value={filter} onChange={(e) => setFilter(e.target.value)} />
        {graphError ? <p className="problems">{graphError}</p>
          : !graph ? <p className="muted">Loading…</p>
          : graph.tables.length === 0 ? <p className="muted">No tables yet.</p>
          : (
            <ul className="db-list" aria-label="Tables">
              {shown.map((t) => (
                <li key={t.name}>
                  <button className="link" aria-current={t.name === table ? "true" : undefined} onClick={() => pickTable(t.name === table ? null : t.name)}>{t.name}</button>
                </li>
              ))}
            </ul>
          )}
      </nav>
      <section className="db-main">
        <h2>{schema}{table ? <> / {table}</> : null}</h2>
        <div className="db-tabs" role="tablist" aria-label="Database sections">
          {TABS.map((t) => (
            <button key={t.id} role="tab" className="tab" aria-selected={tab === t.id} data-active={tab === t.id} onClick={() => setTab(t.id)}>{t.label}</button>
          ))}
        </div>
        {tab === "fields" && <p className="muted">Fields are coming soon.</p>}
        {tab === "relations" && <p className="muted">Relations are coming soon.</p>}
        {tab === "overview" && <p className="muted">Overview is coming soon.</p>}
        {tab === "erd" && <SchemaView schema={schema} refreshKey={refreshKey} />}
      </section>
    </div>
  );
}
