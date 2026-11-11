"use client";

import { useState } from "react";
import type { DataView } from "@/lib/dataRoute";
import { Button, Icon } from "./ui";

interface Props {
  columns: string[];
  view: DataView;
  onChange: (next: DataView) => void;
}

/** Filters and sort as chips above the grid. It never edits `view`; it hands back a changed copy. */
export function FilterBar({ columns, view, onChange }: Props) {
  const [column, setColumn] = useState("");
  const [text, setText] = useState("");
  const col = columns.includes(column) ? column : columns[0] ?? "";
  const entries = Object.entries(view.filters);
  const add = () => {
    if (!col || !text.trim()) return;
    onChange({ ...view, filters: { ...view.filters, [col]: text } });
    setText("");
  };
  const without = (name: string) => {
    const { [name]: _gone, ...rest } = view.filters;
    return rest;
  };
  return (
    <div className="filter-bar" role="group" aria-label="Filter and sort">
      <select aria-label="Filter column" value={col} onChange={(e) => setColumn(e.target.value)}>
        {columns.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>
      <input aria-label="Filter text" placeholder="contains…" value={text} onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") add(); }} />
      <Button size="sm" icon="plus" onClick={add}>Add filter</Button>
      {entries.map(([name, t]) => (
        <span className="chip" key={name}>
          <span>{`${name} contains '${t}'`}</span>
          <button aria-label={`Remove filter ${name}`} onClick={() => onChange({ ...view, filters: without(name) })}><Icon name="x" size={12} /></button>
        </span>
      ))}
      {view.sort && (
        <span className="chip">
          <span>{`Sorted by ${view.sort} ${view.dir === "desc" ? "↓" : "↑"}`}</span>
          <button aria-label="Remove sort" onClick={() => onChange({ ...view, sort: null, dir: "asc" })}><Icon name="x" size={12} /></button>
        </span>
      )}
      {(entries.length > 0 || view.sort) && (
        <Button size="sm" variant="ghost" onClick={() => onChange({ ...view, sort: null, dir: "asc", filters: {} })}>Clear all</Button>
      )}
    </div>
  );
}
