"use client";

import type { Me, SchemaGraph } from "@/lib/api";
import { SearchBox } from "./SearchBox";
import { Button, Panel, Skeleton } from "./ui";

function Stat({ value, label }: { value: number; label: string }) {
  const text = value.toLocaleString();
  return (
    <div className="stat-tile" role="group" aria-label={`${text} ${label}`}>
      <strong>{text}</strong>
      <span>{label}</span>
    </div>
  );
}

const plural = (n: number, one: string, many: string) => (n === 1 ? one : many);

export function OverviewTab({ me, schema, comment, graph, onAskAssistant, onOpenTable }: {
  me: Me; schema: string; comment?: string | null; graph: SchemaGraph | null; onAskAssistant: (text: string) => void;
  onOpenTable: (name: string) => void;
}) {
  const tables = graph?.tables ?? [];
  const columns = tables.reduce((n, t) => n + t.columns.length, 0);
  const relations = graph?.relations.length ?? 0;
  const rows = tables.reduce((n, t) => n + (t.rowEstimate ?? 0), 0);
  return (
    <div className="overview-tab">
      {comment && <p className="muted">{comment}</p>}
      {graph ? (
        <div className="stat-tiles">
          <Stat value={tables.length} label={plural(tables.length, "table", "tables")} />
          <Stat value={columns} label={plural(columns, "column", "columns")} />
          <Stat value={relations} label={plural(relations, "relation", "relations")} />
          <Stat value={rows} label="rows (approx.)" />
        </div>
      ) : (
        <Skeleton rows={2} />
      )}
      <Panel title="Tables">
        {!graph ? <Skeleton rows={3} /> : tables.length === 0 ? <p className="muted">No tables yet.</p> : (
          <ul className="overview-tables">
            {tables.map((t) => (
              <li key={t.name}>
                <button className="overview-table" onClick={() => onOpenTable(t.name)}>
                  <span className="overview-table-name">{t.name}</span>
                  <span className="muted small">
                    {t.columns.length} {plural(t.columns.length, "column", "columns")} · {(t.rowEstimate ?? 0).toLocaleString()} {plural(t.rowEstimate ?? 0, "row", "rows")}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Panel>
      <SearchBox
        key={schema}
        schema={schema}
        canPropose={me.role !== "viewer"}
        canReindex={me.role === "approver"}
        onAskAssistant={onAskAssistant}
      />
      {me.role !== "viewer" && (
        <p>
          <Button
            icon="chat"
            onClick={() => onAskAssistant(`Tell me about the ${schema} database and suggest what to add or improve.`)}
          >Ask the assistant about {schema}</Button>
        </p>
      )}
    </div>
  );
}
