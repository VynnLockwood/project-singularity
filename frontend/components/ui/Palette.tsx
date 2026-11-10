"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { rank, type Command } from "@/lib/palette";
import { Icon } from "./Icon";
import { Kbd } from "./feedback";

interface Props { open: boolean; onClose: () => void; commands: Command[]; placeholder?: string }

/** Type to find a place or an action. Arrow keys move, Enter runs, Escape closes. */
export function Palette({ open, onClose, commands, placeholder = "Search or jump to" }: Props) {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const results = useMemo(() => rank(query, commands), [query, commands]);

  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setQuery("");
    setIndex(0);
    input.current?.focus();
    return () => { if (opener?.isConnected) opener.focus(); };
  }, [open]);

  useEffect(() => { setIndex(0); }, [query]);
  useEffect(() => { list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView?.({ block: "nearest" }); }, [index]);

  if (!open) return null;
  const selected = Math.min(index, Math.max(0, results.length - 1));
  const run = (c: Command | undefined, shift: boolean) => { if (c) { c.run({ shift }); onClose(); } };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { e.preventDefault(); onClose(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); setIndex(results.length ? (selected + 1) % results.length : 0); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setIndex(results.length ? (selected - 1 + results.length) % results.length : 0); }
    else if (e.key === "Enter") { e.preventDefault(); run(results[selected], e.shiftKey); }
    else if (e.key === "Tab") e.preventDefault(); // one control: keep focus on it
  };

  return (
    <div className="ui-palette-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="ui-palette" role="dialog" aria-modal="true" aria-label="Command palette">
        <div className="ui-palette-input">
          <Icon name="search" size={18} />
          <input
            ref={input} role="combobox" aria-expanded="true" aria-controls="ui-palette-list" aria-autocomplete="list"
            aria-activedescendant={results[selected] ? `ui-palette-${selected}` : undefined} aria-label={placeholder}
            placeholder={placeholder} value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={onKeyDown}
            autoComplete="off" spellCheck={false}
          />
        </div>
        <ul ref={list} id="ui-palette-list" className="ui-palette-list" role="listbox" aria-label="Results">
          {results.map((c, i) => (
            <li
              key={c.id} id={`ui-palette-${i}`} role="option" aria-selected={i === selected}
              data-first-of-group={i === 0 || results[i - 1].group !== c.group ? c.group : undefined}
              onMouseMove={() => setIndex(i)} onMouseDown={(e) => e.preventDefault()} onClick={(e) => run(c, e.shiftKey)}
            >
              <Icon name={c.icon} />
              <span className="ui-palette-label">{c.label}</span>
              {c.hint && <span className="ui-palette-hint">{c.hint}</span>}
            </li>
          ))}
        </ul>
        {results.length === 0 && <p className="ui-palette-none">No matches</p>}
        <footer className="ui-palette-foot">
          <span><Kbd>↑</Kbd> <Kbd>↓</Kbd> move</span>
          <span><Kbd>Enter</Kbd> open</span>
          <span><Kbd>Shift</Kbd> <Kbd>Enter</Kbd> open a table in Data</span>
          <span><Kbd>Esc</Kbd> close</span>
        </footer>
      </div>
    </div>
  );
}
