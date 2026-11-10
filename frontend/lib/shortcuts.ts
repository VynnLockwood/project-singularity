"use client";

import { useEffect, useRef } from "react";

/** True when a key press belongs to text entry, so single-key shortcuts must stay out of the way. */
export function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return true;
  const editable = target.getAttribute("contenteditable");
  return target.isContentEditable === true || editable === "" || editable === "true";
}

const SEQUENCE_MS = 1000;

/**
 * Runs `handler` on a keyboard shortcut.
 * - `"mod+k"`: Ctrl or Meta plus a key. Fires anywhere, including while typing.
 * - `"g r"`: two plain keys in a row within a second. Never fires while typing.
 */
export function useShortcut(keys: string, handler: () => void): void {
  const run = useRef(handler);
  run.current = handler;

  useEffect(() => {
    const chord = keys.startsWith("mod+") ? keys.slice(4).toLowerCase() : null;
    const [first, second] = chord ? [] : keys.toLowerCase().split(" ");
    let armedAt = -Infinity;

    const onKey = (e: KeyboardEvent) => {
      const k = e.key.toLowerCase();
      if (chord) {
        if ((e.ctrlKey || e.metaKey) && !e.altKey && k === chord) { e.preventDefault(); run.current(); }
        return;
      }
      if (e.ctrlKey || e.metaKey || e.altKey || isTyping(e.target)) { armedAt = -Infinity; return; }
      const now = Date.now();
      if (k === second && now - armedAt <= SEQUENCE_MS) { armedAt = -Infinity; e.preventDefault(); run.current(); return; }
      armedAt = k === first ? now : -Infinity;
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [keys]);
}
