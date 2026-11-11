"use client";

import { useState } from "react";
import { Icon } from "./ui";

interface Props {
  tables: { name: string; rowEstimate?: number }[];
  selected: string;
  onSelect: (name: string) => void;
  searchLabel: string;
  emptyText: string;
  noMatchText: string;
  /** Show the row estimate beside each name (and in its accessible name). */
  estimates?: boolean;
  listLabel?: string;
}

/** A search box over a list of tables. The caller decides what choosing one does (and closes its own disclosure). */
export function TableList({ tables, selected, onSelect, searchLabel, emptyText, noMatchText, estimates = true, listLabel }: Props) {
  const [q, setQ] = useState("");
  const shown = tables.filter((t) => t.name.toLowerCase().includes(q.trim().toLowerCase()));
  return (
    <>
      <input type="search" aria-label={searchLabel} placeholder={searchLabel} value={q} onChange={(e) => setQ(e.target.value)} />
      {tables.length === 0 ? <p className="muted small">{emptyText}</p>
        : shown.length === 0 ? <p className="muted small">{noMatchText}</p>
        : (
          <ul className="table-list" aria-label={listLabel}>
            {shown.map((t) => (
              <li key={t.name}>
                <button aria-current={t.name === selected ? "true" : undefined} onClick={() => onSelect(t.name)}>
                  {estimates && <Icon name="table" size={14} />}
                  <span>{t.name}</span>
                  {estimates && t.rowEstimate ? <span className="muted small">{t.rowEstimate.toLocaleString()}</span> : null}
                </button>
              </li>
            ))}
          </ul>
        )}
    </>
  );
}
