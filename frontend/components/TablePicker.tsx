"use client";

import { useRef, useState } from "react";
import type { SchemaInfo } from "@/lib/api";
import { Icon } from "./ui";

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
  const [q, setQ] = useState("");
  const box = useRef<HTMLDetailsElement>(null);
  const shown = tables.filter((t) => t.name.toLowerCase().includes(q.trim().toLowerCase()));
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
      <input type="search" aria-label="Search tables" placeholder="Search tables" value={q} onChange={(e) => setQ(e.target.value)} />
      {tables.length === 0 ? <p className="muted small">No tables in this database.</p>
        : shown.length === 0 ? <p className="muted small">No table matches.</p>
        : (
          <ul className="table-list">
            {shown.map((t) => (
              <li key={t.name}>
                <button aria-current={t.name === table ? "true" : undefined}
                  onClick={() => { onTable(t.name); if (box.current && (typeof matchMedia === "function" && matchMedia("(max-width: 800px)").matches)) box.current.open = false; }}>
                  <Icon name="table" size={14} />
                  <span>{t.name}</span>
                  {t.rowEstimate ? <span className="muted small">{t.rowEstimate.toLocaleString()}</span> : null}
                </button>
              </li>
            ))}
          </ul>
        )}
    </details>
  );
}
