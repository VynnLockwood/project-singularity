"use client";

import { useEffect, useRef, type ReactNode } from "react";

const FOCUSABLE = 'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

/**
 * A side region that slides in next to the page. It is not a modal: there is no backdrop and no
 * focus trap, so the page behind stays usable. Focus moves in on open and back out on close.
 */
export function Tray({ open, onClose, label, children }: { open: boolean; onClose: () => void; label: string; children: ReactNode }) {
  const ref = useRef<HTMLElement>(null);
  const opener = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    (ref.current?.querySelector<HTMLElement>(FOCUSABLE) ?? ref.current)?.focus();
    return () => { if (opener.current?.isConnected) opener.current.focus(); };
  }, [open]);

  // If the focused control inside the tray goes away (its card was decided elsewhere), keep focus in the tray.
  useEffect(() => {
    if (open && ref.current && (document.activeElement === document.body || document.activeElement === null)) ref.current.focus();
  });

  if (!open) return null;
  return (
    <aside
      ref={ref} className="ui-tray" role="complementary" aria-label={label} tabIndex={-1}
      onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } }}
    >
      {children}
    </aside>
  );
}
