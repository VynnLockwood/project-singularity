"use client";

import { useEffect, useRef, useState } from "react";
import { getGraph, getSchemas, type Change, type Me, type SchemaInfo } from "@/lib/api";
import { parseView, viewToSearch, type DataView as View } from "@/lib/dataRoute";
import type { Route } from "@/lib/route";
import type { Navigate } from "@/lib/useRoute";
import { EmptyState, Skeleton } from "@/components/ui";
import { GridEditor } from "./GridEditor";
import { TablePicker } from "./TablePicker";

interface Props {
  me: Me;
  live: Change[];
  route: Route;
  navigate: Navigate;
  refreshKey: number;
  onProposed?: (change: Change) => void;
  onBusy?: (b: boolean) => void;
}

interface Listed { schema: string; tables: { name: string; rowEstimate?: number; columns: string[] }[] }

/** The Data tab: a database and table picker on the left, the grid beside it. The address is `/data/<db>/<table>?…`. */
export function DataView({ me, live, route, navigate, refreshKey, onProposed, onBusy }: Props) {
  const [schemas, setSchemas] = useState<SchemaInfo[] | null>(null);
  const [listed, setListed] = useState<Listed | null>(null);
  const [error, setError] = useState("");
  const [held, setHeld] = useState<{ key: string; view: View } | null>(null);
  const lastGood = useRef("");

  const wantedSchema = route.view === "data" ? route.schema : undefined;
  const wantedTable = route.view === "data" ? route.table : undefined;
  const db = wantedSchema ?? (schemas ? (schemas.find((s) => s.tables > 0) ?? schemas[0])?.name : undefined);
  const dbKnown = !!schemas && !!db && schemas.some((s) => s.name === db);

  useEffect(() => {
    getSchemas().then((s) => { setSchemas(s); setError(""); }).catch(() => setError("Could not load the databases."));
  }, [refreshKey]);

  useEffect(() => {
    if (!db || !dbKnown) { setListed(null); return; }
    let cancelled = false;
    getGraph(db)
      .then((g) => { if (!cancelled) setListed({ schema: db, tables: g.tables.map((t) => ({ name: t.name, rowEstimate: t.rowEstimate, columns: t.columns.map((c) => c.name) })) }); })
      .catch(() => { if (!cancelled) setError("Could not load the tables."); });
    return () => { cancelled = true; };
  }, [db, dbKnown, refreshKey]);

  const tables = listed && listed.schema === db ? listed.tables : null;
  const exists = !!wantedTable && !!tables?.some((t) => t.name === wantedTable);

  // Nothing picked yet: take the first table, replacing the address. A table that was there and is gone: same.
  useEffect(() => {
    if (!tables || !db) return;
    const first = tables[0];
    if (!wantedTable) {
      if (first) navigate({ view: "data", schema: db, table: first.name }, { replace: true });
    } else if (!exists && lastGood.current === `${db}/${wantedTable}`) {
      navigate(first ? { view: "data", schema: db, table: first.name } : { view: "data", schema: db }, { replace: true });
    }
  }, [tables, db, wantedTable, exists, navigate]);

  useEffect(() => { if (exists) lastGood.current = `${db}/${wantedTable}`; }, [exists, db, wantedTable]);

  // The view belongs to one table. A table we have not held a view for starts from what the address says
  // (a clicked table has no query, a shared link has one), worked out while rendering so no stale view is ever used.
  const key = `${db}/${wantedTable}`;
  const columns = tables?.find((t) => t.name === wantedTable)?.columns ?? [];
  const raw = held?.key === key ? held.view : parseView(typeof location === "undefined" ? "" : location.search);
  // A hand-edited link may name columns the table does not have: drop them rather than ask the server.
  const view: View = {
    ...raw,
    sort: raw.sort && columns.includes(raw.sort) ? raw.sort : null,
    filters: Object.fromEntries(Object.entries(raw.filters).filter(([c]) => columns.includes(c))),
  };
  const setView = (next: View) => {
    history.replaceState(null, "", location.pathname + viewToSearch(next));
    setHeld({ key, view: next });
  };

  if (error) return <p className="problems" role="alert">{error}</p>;
  if (!schemas) return <div role="status"><span className="sr-only">Loading…</span><Skeleton rows={6} /></div>;
  if (schemas.every((s) => s.tables === 0) && !wantedSchema) {
    return <EmptyState icon="database" title="No tables yet.">Ask the assistant to design one in the Chat tab.</EmptyState>;
  }

  const message = !dbKnown ? `There is no database ${db}.`
    : tables && wantedTable && !exists ? `There is no table ${wantedTable} in ${db}.`
    : tables && tables.length === 0 ? "No tables in this database."
    : "";

  return (
    <div className="data-view">
      <TablePicker
        schemas={schemas} schema={db ?? ""} tables={tables ?? []} table={exists ? wantedTable! : ""}
        onSchema={(name) => navigate({ view: "data", schema: name })}
        onTable={(name) => navigate({ view: "data", schema: db!, table: name })}
      />
      <div className="data-main">
        {message ? <p className="muted" role="status">{message}</p>
          : exists ? (
            <GridEditor key={`${db}/${wantedTable}`} me={me} schema={db} table={wantedTable!} live={live}
              view={view} onViewChange={setView} onProposed={onProposed} onBusy={onBusy} />
          ) : <Skeleton rows={6} />}
      </div>
    </div>
  );
}
