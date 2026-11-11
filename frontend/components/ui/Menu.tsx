"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "./Button";

export interface MenuItem { label: string; ariaLabel?: string; danger?: boolean; onSelect(): void }

/** A small "more actions" menu. Closes on choosing an item, Escape (focus goes back to the trigger) or a click outside. */
export function Menu({ label, items }: { label: string; items: MenuItem[] }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const outside = (e: MouseEvent) => { if (root.current && !root.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [open]);

  return (
    <span className="ui-menu" ref={root} onKeyDown={(e) => { if (e.key === "Escape" && open) { setOpen(false); root.current?.querySelector<HTMLButtonElement>("button")?.focus(); } }}>
      <Button size="sm" variant="ghost" icon="more" aria-label={label} aria-haspopup="true" aria-expanded={open} onClick={() => setOpen((o) => !o)} />
      {open && (
        <span className="ui-menu-list">
          {items.map((it) => (
            <button
              key={it.label}
              type="button"
              className="ui-menu-item"
              aria-label={it.ariaLabel ?? it.label}
              data-danger={it.danger ? "true" : undefined}
              onClick={() => { setOpen(false); it.onSelect(); }}
            >{it.label}</button>
          ))}
        </span>
      )}
    </span>
  );
}
