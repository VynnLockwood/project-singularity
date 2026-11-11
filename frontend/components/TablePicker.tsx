"use client";

import { useRef } from "react";
import type { SchemaInfo } from "@/lib/api";
import { TableList } from "./TableList";

interface Props {
  schemas: SchemaInfo[];
  schema: string;
  tables: { name: string; rowEstimate?: number }[];
  table: string;
  onSchema: (name: string) => void;
  onTable: (name: string) => void;
}

/** Database select, table search and the table list. Under 800px the CSS shows only the "Tables" disclosure. */
export function TablePicker({ schemas, schema, tables, table, onSchema, onTable }: Props) {
  const box = useRef<HTMLDetailsElement>(null);
  const names = schemas.some((s) => s.name === schema) ? schemas : [...schemas, { name: schema, tables: 0, comment: null }];
  return (
    <details className="table-picker" ref={box} open>
      <summary>Tables</summary>
      <label className="small">
        Database
        <select aria-label="Database" value={schema} onChange={(e) => onSchema(e.target.value)}>
          {names.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
        </select>
      </label>
      <TableList
        tables={tables} selected={table} searchLabel="Search tables"
        emptyText="No tables in this database." noMatchText="No table matches."
        onSelect={(name) => { onTable(name); if (box.current && (typeof matchMedia === "function" && matchMedia("(max-width: 800px)").matches)) box.current.open = false; }}
      />
    </details>
  );
}
