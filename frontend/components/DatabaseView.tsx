"use client";

import { useEffect, useRef, useState } from "react";
import { getGraph, getSchemas, type Change, type Me, type SchemaGraph, type SchemaInfo } from "@/lib/api";
import type { Route } from "@/lib/route";
import type { Navigate } from "@/lib/useRoute";
import { parseTab, tabToSearch, type DbTab } from "@/lib/databaseRoute";
import { SchemaView } from "./SchemaView";
import { NewDatabaseForm } from "./NewDatabaseForm";
import { ProposeDialog } from "./ProposeDialog";
import { dropSchemaOp, dropTableOp, renameSchemaOp, renameTableOp, validateName, validateSchemaName } from "@/lib/databaseOps";
import { NewTableForm } from "./NewTableForm";
import { FieldsTab } from "./FieldsTab";
import { RelationsTab } from "./RelationsTab";
import { ExportMenu } from "./ExportMenu";
import { OverviewTab } from "./OverviewTab";
import { TableList } from "./TableList";
import { Button } from "./ui";

export type { DbTab };

const TABS: { id: DbTab; label: string }[] = [
  { id: "fields", label: "Fields" },
  { id: "relations", label: "Relations" },
  { id: "erd", label: "ERD" },
  { id: "overview", label: "Overview" },
];

type Panel = "new" | "rename" | "rename-review" | "drop" | "trename" | "trename-review" | "tdrop" | "newtable" | null;

export function DatabaseView({ me, live, route, navigate, refreshKey, onProposed, onAskAssistant }: {
  me: Me; live: Change[]; route: Route; navigate: Navigate; refreshKey: number;
  onProposed: (c: Change) => void; onAskAssistant: (text: string) => void;
}) {
  const addrSchema = route.view === "database" ? route.schema : undefined;
  const addrTable = route.view === "database" ? route.table : undefined;
  const [schemas, setSchemas] = useState<SchemaInfo[] | null>(null);
  const [schemasError, setSchemasError] = useState("");
  const [graphState, setGraphState] = useState<{ schema: string; graph: SchemaGraph } | null>(null);
  const [graphFail, setGraphFail] = useState<{ schema: string; msg: string } | null>(null);
  const [tabState, setTabState] = useState<{ key: string; tab: DbTab } | null>(null);
  const [panel, setPanel] = useState<Panel>(null);
  const [newName, setNewName] = useState("");
  const [target, setTarget] = useState("");
  const seq = useRef(0);
  const panelRef = useRef<Panel>(null);
  panelRef.current = panel;
  const tableRef = useRef(addrTable);
  tableRef.current = addrTable;
  const schemaRef = useRef<string | null>(null);
  const created = useRef<{ name: string; id: number } | null>(null);
  const lastSchema = useRef<string | null>(null);
  const lastTable = useRef<string | null>(null);
  const rail = useRef<HTMLDetailsElement>(null);

  const first = schemas && schemas.length ? (schemas.some((x) => x.name === "master") ? "master" : schemas[0].name) : undefined;
  const wanted = addrSchema ?? first; // /database means "the first database" before the address catches up
  const known = !!wanted && !!schemas && schemas.some((x) => x.name === wanted);
  const schema = known ? wanted! : null;
  schemaRef.current = schema;
  const graph = schema && graphState?.schema === schema ? graphState.graph : null;
  const graphError = schema && graphFail?.schema === schema ? graphFail.msg : "";
  const tableOk = !!(graph && addrTable && graph.tables.some((t) => t.name === addrTable));
  const table = tableOk ? addrTable! : null;

  const go = (name: string, tbl?: string, tab?: DbTab, replace?: boolean) => {
    navigate({ view: "database", schema: name, ...(tbl ? { table: tbl } : {}) }, replace ? { replace: true } : undefined);
    const q = tab ? tabToSearch(tab, !!tbl) : "";
    if (q) history.replaceState(null, "", location.pathname + q);
    setTabState({ key: `${name}/${tbl ?? ""}`, tab: tab ?? parseTab("", !!tbl) });
  };

  const key = `${schema ?? ""}/${table ?? ""}`;
  const tab: DbTab = tabState?.key === key ? tabState.tab : parseTab(typeof location === "undefined" ? "" : location.search, !!table);
  const chooseTab = (t: DbTab) => {
    setTabState({ key, tab: t });
    history.replaceState(null, "", location.pathname + tabToSearch(t, !!table));
  };

  // A remembered create that was rejected/failed/went stale must not auto-select later.
  useEffect(() => {
    const cur = created.current;
    if (cur && live.some((c) => c.id === cur.id && (c.status === "rejected" || c.status === "failed" || c.status === "stale"))) {
      created.current = null;
    }
  }, [live]);

  useEffect(() => {
    let current = true;
    getSchemas()
      .then((list) => {
        if (!current) return;
        setSchemas(list);
        setSchemasError("");
        if (created.current && list.some((x) => x.name === created.current!.name)) {
          const n = created.current.name;
          created.current = null;
          // Only take over the view when the user is not in the middle of something else.
          const idle = (panelRef.current === "new" || panelRef.current === null) && !tableRef.current;
          if (idle) {
            setPanel((p) => (p === "new" ? null : p));
            if (n !== schemaRef.current) go(n);
          }
        }
      })
      .catch(() => { if (current) setSchemasError("Could not load the databases."); });
    return () => { current = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  // The address names no database, or one that has gone: land on a real one.
  useEffect(() => {
    if (!schemas || schemas.length === 0) return;
    if (!addrSchema) go(first!, undefined, undefined, true);
    else if (!known && lastSchema.current === addrSchema) go(first!, undefined, undefined, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schemas, addrSchema, known]);
  useEffect(() => { if (known) lastSchema.current = addrSchema!; }, [known, addrSchema]);

  // Panels belong to the place they were opened in.
  useEffect(() => {
    setPanel((p) => (p === "new" || p === "rename-review" || p === "drop" ? p : null));
  }, [schema]);
  useEffect(() => {
    setPanel((p) => (p === "trename" || p === "trename-review" || p === "tdrop" ? null : p));
  }, [table]);

  useEffect(() => {
    if (!schema) return;
    const id = ++seq.current;
    setGraphFail(null);
    getGraph(schema)
      .then((g) => { if (id === seq.current) setGraphState({ schema, graph: g }); })
      .catch(() => { if (id === seq.current) setGraphFail({ schema, msg: "Could not load the tables." }); });
  }, [schema, refreshKey]);

  // A table that was there and has gone: fall back to its database.
  useEffect(() => {
    if (!graph || !addrTable) return;
    const here = `${schema}/${addrTable}`;
    if (graph.tables.some((t) => t.name === addrTable)) lastTable.current = here;
    else if (lastTable.current === here) go(schema!, undefined, undefined, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph, addrTable]);

  const closeRail = () => {
    if (typeof matchMedia === "function" && matchMedia("(max-width: 800px)").matches && rail.current) rail.current.open = false;
  };

  const pickSchema = (name: string) => {
    if (name === schema) return;
    setPanel(null);
    created.current = null;
    go(name, undefined, tab === "erd" ? "erd" : undefined);
    closeRail();
  };

  const pickTable = (name: string) => {
    created.current = null;
    if (name === table) go(schema!);
    else go(schema!, name, "fields");
    closeRail();
  };

  const info = (schemas ?? []).find((x) => x.name === schema);
  const canWrite = me.role !== "viewer";
  const canManage = canWrite && schema !== "master" && !!info;
  const hasTables = (info?.tables ?? 0) > 0;
  const taken = (schemas ?? []).map((x) => x.name);
  const renameError = newName === "" ? null : validateSchemaName(newName, taken);
  const tableRenameError = newName === "" ? null : validateName(newName, (graph?.tables ?? []).map((t) => t.name));
  const closePanel = () => { setPanel(null); setNewName(""); };

  const noDatabase = !!schemas && !!addrSchema && !known && lastSchema.current !== addrSchema;
  const noTable = !!graph && !!addrTable && !tableOk && lastTable.current !== `${schema}/${addrTable}`;

  return (
    <div className="db-layout">
      <nav className="db-rail" aria-label="Databases">
        <details className="db-rail-box" open ref={rail}>
          <summary>Databases and tables</summary>
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
              <Button size="sm" icon="plus" onClick={() => { setNewName(""); setPanel("new"); }}>New database</Button>
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
          {canWrite && graph && panel !== "newtable" && (
            <Button size="sm" icon="plus" onClick={() => { created.current = null; setPanel("newtable"); }}>New table</Button>
          )}
          {canWrite && graph && schema && panel === "newtable" && (
            <NewTableForm
              key={schema}
              me={me}
              schema={schema}
              tables={graph.tables}
              onClose={closePanel}
              onProposed={(c) => onProposed(c)}
            />
          )}
          {graphError ? <p className="problems">{graphError}</p>
            : !graph ? <p className="muted">{schema ? "Loading…" : ""}</p>
            : (
              <TableList
                key={`list-${schema}`}
                tables={graph.tables}
                selected={table ?? ""}
                onSelect={pickTable}
                searchLabel="Filter tables"
                emptyText="No tables yet."
                noMatchText="No matching tables."
                estimates={false}
                listLabel="Tables"
              />
            )}
        </details>
      </nav>
      <section className="db-main">
        {noDatabase && <p className="muted">There is no database {addrSchema}.</p>}
        {!noDatabase && !schema && <p className="muted">Loading…</p>}
        {schema && (
          <>
        <div className="grid-bar">
          <h2>{schema}{table ? <> / {table}</> : null}</h2>
          <div className="db-actions">
            {canManage && (
              <>
                <Button size="sm" onClick={() => { created.current = null; setNewName(""); setTarget(schema); setPanel("rename"); }}>Rename</Button>
                <Button
                  size="sm"
                  variant="danger"
                  disabled={hasTables}
                  title={hasTables ? "Only empty databases can be deleted" : undefined}
                  onClick={() => { created.current = null; setTarget(schema); setPanel("drop"); }}
                >Delete</Button>
              </>
            )}
            {canWrite && table && (
              <>
                <Button size="sm" onClick={() => { created.current = null; setNewName(""); setPanel("trename"); }}>Rename table</Button>
                <Button size="sm" variant="danger" onClick={() => { created.current = null; setPanel("tdrop"); }}>Drop table</Button>
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
            onBack={() => setPanel("rename")}
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
            build={() => [renameTableOp(table, newName)]} onClose={closePanel} onBack={() => setPanel("trename")} onProposed={onProposed} />
        )}
        {canWrite && table && panel === "tdrop" && (
          <ProposeDialog me={me} schema={schema} destructive title={`Drop table ${table}`}
            build={(reason) => [dropTableOp(table, reason)]} onClose={closePanel} onProposed={onProposed} />
        )}
        <div className="db-tabs" role="tablist" aria-label="Database sections">
          {TABS.map((t) => (
            <button key={t.id} role="tab" className="tab" aria-selected={tab === t.id} data-active={tab === t.id} onClick={() => chooseTab(t.id)}>{t.label}</button>
          ))}
        </div>
        {noTable && <p className="muted">There is no table {addrTable} in {schema}.</p>}
        {tab === "fields" && (table
          ? <FieldsTab key={`${schema}/${table}`} me={me} schema={schema} table={table} refreshKey={refreshKey} onProposed={onProposed} />
          : <p className="muted">Select a table to see its fields.</p>)}
        {tab === "relations" && (table
          ? <RelationsTab key={`${schema}/${table}`} me={me} schema={schema} table={table} graph={graph} refreshKey={refreshKey} onProposed={onProposed} />
          : <p className="muted">Select a table to see its relations.</p>)}
        {tab === "overview" && <OverviewTab me={me} schema={schema} comment={info?.comment} graph={graph} onAskAssistant={onAskAssistant} />}
        {tab === "erd" && <SchemaView schema={schema} refreshKey={refreshKey} />}
          </>
        )}
      </section>
    </div>
  );
}
