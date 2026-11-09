"use client";

import { useEffect, useRef, useState } from "react";
import { getGraph, getSchemas, type Change, type Me, type SchemaGraph, type SchemaInfo } from "@/lib/api";
import { SchemaView } from "./SchemaView";
import { NewDatabaseForm } from "./NewDatabaseForm";
import { ProposeDialog } from "./ProposeDialog";
import { dropSchemaOp, dropTableOp, renameSchemaOp, renameTableOp, validateName, validateSchemaName } from "@/lib/databaseOps";
import { FieldsTab } from "./FieldsTab";
import { ExportMenu } from "./ExportMenu";

export type DbTab = "fields" | "relations" | "erd" | "overview";

const TABS: { id: DbTab; label: string }[] = [
  { id: "fields", label: "Fields" },
  { id: "relations", label: "Relations" },
  { id: "erd", label: "ERD" },
  { id: "overview", label: "Overview" },
];

export function DatabaseView({ me, live, refreshKey, onProposed }: {
  me: Me; live: Change[]; refreshKey: number;
  onProposed: (c: Change) => void; onAskAssistant: (text: string) => void;
}) {
  const [schemas, setSchemas] = useState<SchemaInfo[] | null>(null);
  const [schemasError, setSchemasError] = useState("");
  const [schema, setSchema] = useState("master");
  const [graph, setGraph] = useState<SchemaGraph | null>(null);
  const [graphError, setGraphError] = useState("");
  const [table, setTable] = useState<string | null>(null);
  const [tab, setTab] = useState<DbTab>("overview");
  const [filter, setFilter] = useState("");
  const [panel, setPanel] = useState<"new" | "rename" | "rename-review" | "drop" | "trename" | "trename-review" | "tdrop" | null>(null);
  const [newName, setNewName] = useState("");
  const [target, setTarget] = useState("");
  const seq = useRef(0);
  const schemaRef = useRef(schema);
  schemaRef.current = schema;
  const created = useRef<{ name: string; id: number } | null>(null);

  // A remembered create that was rejected/failed/went stale must not auto-select later.
  useEffect(() => {
    const cur = created.current;
    if (cur && live.some((c) => c.id === cur.id && (c.status === "rejected" || c.status === "failed" || c.status === "stale"))) {
      created.current = null;
    }
  }, [live]);

  // One reset for every schema change (user pick, created, or the selected one vanished).
  const resetTo = (name: string) => {
    seq.current++;
    setGraph(null);
    setTable(null);
    setTab((t) => (t === "erd" ? t : "overview"));
    setFilter("");
    setPanel((p) => (p === "trename" || p === "trename-review" || p === "tdrop" ? null : p));
    setSchema(name);
  };

  useEffect(() => {
    let current = true;
    getSchemas()
      .then((list) => {
        if (!current) return;
        setSchemas(list);
        setSchemasError("");
        const has = (n: string) => list.some((x) => x.name === n);
        if (created.current && has(created.current.name)) {
          const n = created.current.name;
          created.current = null;
          setPanel((p) => (p === "new" ? null : p));
          if (n !== schemaRef.current) resetTo(n);
        } else if (!has(schemaRef.current)) {
          resetTo("master");
        }
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
    if (graph && table && !graph.tables.some((t) => t.name === table)) {
      setTable(null);
      setTab((t) => (t === "erd" ? t : "overview"));
    }
  }, [graph, table]);

  const pickSchema = (name: string) => {
    if (name === schema) return;
    setPanel(null);
    created.current = null;
    resetTo(name);
  };

  const pickTable = (name: string | null) => {
    setPanel((p) => (p === "trename" || p === "trename-review" || p === "tdrop" ? null : p));
    setTable(name);
    setTab(name ? "fields" : "overview");
  };

  const info = (schemas ?? []).find((x) => x.name === schema);
  const canWrite = me.role !== "viewer";
  const canManage = canWrite && schema !== "master" && !!info;
  const hasTables = (info?.tables ?? 0) > 0;
  const taken = (schemas ?? []).map((x) => x.name);
  const renameError = newName === "" ? null : validateSchemaName(newName, taken);
  const tableRenameError = newName === "" ? null : validateName(newName, (graph?.tables ?? []).map((t) => t.name));
  const closePanel = () => { setPanel(null); setNewName(""); };

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
        <div className="db-rail-actions">
          {canWrite && schemas && panel !== "new" && (
            <button className="button" onClick={() => { setNewName(""); setPanel("new"); }}>New database</button>
          )}
          {canWrite && schemas && panel === "new" && (
            <NewDatabaseForm
              me={me}
              existing={taken}
              onClose={closePanel}
              onProposed={(c, name) => {
                created.current = c.status === "rejected" || c.status === "failed" || c.status === "stale"
                  ? null : { name, id: c.id };
                onProposed(c);
              }}
            />
          )}
        </div>
        <h3>Tables</h3>
        <input aria-label="Filter tables" placeholder="Filter tables" value={filter} onChange={(e) => setFilter(e.target.value)} />
        {graphError ? <p className="problems">{graphError}</p>
          : !graph ? <p className="muted">Loading…</p>
          : graph.tables.length === 0 ? <p className="muted">No tables yet.</p>
          : shown.length === 0 ? <p className="muted">No matching tables.</p>
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
        <div className="grid-bar">
          <h2>{schema}{table ? <> / {table}</> : null}</h2>
          <div>
            {canManage && (
              <>
                <button className="link" onClick={() => { setNewName(""); setTarget(schema); setPanel("rename"); }}>Rename</button>{" "}
                <button
                  className="link"
                  disabled={hasTables}
                  title={hasTables ? "Only empty databases can be deleted" : undefined}
                  onClick={() => { setTarget(schema); setPanel("drop"); }}
                >Delete</button>{" "}
              </>
            )}
            {canWrite && table && (
              <>
                <button className="link" onClick={() => { setNewName(""); setPanel("trename"); }}>Rename table</button>{" "}
                <button className="link" onClick={() => setPanel("tdrop")}>Drop table</button>{" "}
              </>
            )}
            <ExportMenu schema={schema} canExportData={me.role !== "viewer"} />
          </div>
        </div>
        {canManage && panel === "rename" && (
          <form className="db-inline-form" onSubmit={(e) => { e.preventDefault(); if (newName && !renameError) setPanel("rename-review"); }}>
            <label>
              New name
              <input value={newName} onChange={(e) => setNewName(e.target.value)} aria-invalid={renameError ? "true" : undefined} autoFocus />
            </label>
            {renameError && <p className="problems small" role="alert">{renameError}</p>}
            <button type="submit" className="button" disabled={!newName || !!renameError}>Review</button>{" "}
            <button type="button" className="link" onClick={closePanel}>Cancel</button>
          </form>
        )}
        {panel === "rename-review" && (
          <ProposeDialog
            me={me}
            schema="master"
            title={`Rename database ${target} to ${newName}`}
            build={() => [renameSchemaOp(target, newName)]}
            onClose={closePanel}
            onProposed={onProposed}
          />
        )}
        {panel === "drop" && (
          <ProposeDialog
            me={me}
            schema="master"
            title={`Delete database ${target}`}
            destructive
            build={(reason) => [dropSchemaOp(target, reason)]}
            onClose={closePanel}
            onProposed={onProposed}
          />
        )}
        {canWrite && table && panel === "trename" && (
          <form className="db-inline-form" onSubmit={(e) => { e.preventDefault(); if (newName && !tableRenameError) setPanel("trename-review"); }}>
            <label>
              New table name
              <input value={newName} onChange={(e) => setNewName(e.target.value)} aria-invalid={tableRenameError ? "true" : undefined} autoFocus />
            </label>
            {tableRenameError && <p className="problems small" role="alert">{tableRenameError}</p>}
            <button type="submit" className="button" disabled={!newName || !!tableRenameError}>Review</button>{" "}
            <button type="button" className="link" onClick={closePanel}>Cancel</button>
          </form>
        )}
        {canWrite && table && panel === "trename-review" && (
          <ProposeDialog me={me} schema={schema} title={`Rename table ${table} to ${newName}`}
            build={() => [renameTableOp(table, newName)]} onClose={closePanel} onProposed={onProposed} />
        )}
        {canWrite && table && panel === "tdrop" && (
          <ProposeDialog me={me} schema={schema} destructive title={`Drop table ${table}`}
            build={(reason) => [dropTableOp(table, reason)]} onClose={closePanel} onProposed={onProposed} />
        )}
        <div className="db-tabs" role="tablist" aria-label="Database sections">
          {TABS.map((t) => (
            <button key={t.id} role="tab" className="tab" aria-selected={tab === t.id} data-active={tab === t.id} onClick={() => setTab(t.id)}>{t.label}</button>
          ))}
        </div>
        {tab === "fields" && (table
          ? <FieldsTab key={`${schema}/${table}`} me={me} schema={schema} table={table} refreshKey={refreshKey} onProposed={onProposed} />
          : <p className="muted">Select a table to see its fields.</p>)}
        {tab === "relations" && <p className="muted">Relations are coming soon.</p>}
        {tab === "overview" && <p className="muted">Overview is coming soon.</p>}
        {tab === "erd" && <SchemaView schema={schema} refreshKey={refreshKey} />}
      </section>
    </div>
  );
}
