"use client";

import type { Me, SchemaGraph } from "@/lib/api";
import { SearchBox } from "./SearchBox";

export function OverviewTab({ me, schema, comment, graph, onAskAssistant }: {
  me: Me; schema: string; comment?: string | null; graph: SchemaGraph | null; onAskAssistant: (text: string) => void;
}) {
  const tables = graph?.tables.length ?? 0;
  const columns = (graph?.tables ?? []).reduce((n, t) => n + t.columns.length, 0);
  return (
    <div>
      <h3>{schema}</h3>
      {comment && <p className="muted">{comment}</p>}
      {graph ? (
        <p className="muted small"><span>{tables} {tables === 1 ? "table" : "tables"}</span> · <span>{columns} {columns === 1 ? "column" : "columns"}</span></p>
      ) : (
        <p className="muted small">Loading…</p>
      )}
      <SearchBox
        key={schema}
        schema={schema}
        canPropose={me.role !== "viewer"}
        canReindex={me.role === "approver"}
        onAskAssistant={onAskAssistant}
      />
      {me.role !== "viewer" && (
        <p>
          <button
            className="button"
            onClick={() => onAskAssistant(`Tell me about the ${schema} database and suggest what to add or improve.`)}
          >Ask the assistant about {schema}</button>
        </p>
      )}
    </div>
  );
}
