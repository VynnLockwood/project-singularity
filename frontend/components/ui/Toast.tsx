"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Icon, type IconName } from "./Icon";

export const MAX_TOASTS = 3;
export const TOAST_MS = 5000;

export interface ToastOpts { action?: { label: string; onClick: () => void } }
type Kind = "success" | "error" | "info";
interface Item extends ToastOpts { id: number; kind: Kind; message: string }
export interface ToastApi {
  success(message: string, opts?: ToastOpts): void;
  error(message: string, opts?: ToastOpts): void;
  info(message: string, opts?: ToastOpts): void;
}

const NOOP: ToastApi = { success() {}, error() {}, info() {} };
const Ctx = createContext<ToastApi>(NOOP);
const ICON: Record<Kind, IconName> = { success: "check", error: "alert", info: "dot" };

/** Short messages in the corner. Errors stay until dismissed; the rest leave after five seconds. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Item[]>([]);
  const next = useRef(1);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: number) => {
    clearTimeout(timers.current.get(id));
    timers.current.delete(id);
    setItems((list) => list.filter((t) => t.id !== id));
  }, []);

  const push = useCallback((kind: Kind, message: string, opts?: ToastOpts) => {
    const id = next.current++;
    // a burst of events must never pile up over the page: only the newest few stay
    setItems((list) => [...list, { id, kind, message, ...opts }].slice(-MAX_TOASTS));
    if (kind !== "error") timers.current.set(id, setTimeout(() => dismiss(id), TOAST_MS));
  }, [dismiss]);

  useEffect(() => {
    const pending = timers.current;
    return () => { pending.forEach(clearTimeout); pending.clear(); };
  }, []);

  const api = useMemo<ToastApi>(() => ({
    success: (m, o) => push("success", m, o),
    error: (m, o) => push("error", m, o),
    info: (m, o) => push("info", m, o),
  }), [push]);

  return (
    <Ctx.Provider value={api}>
      {children}
      <div className="ui-toasts">
        {items.map((t) => (
          <div key={t.id} className="ui-toast" data-kind={t.kind} role={t.kind === "error" ? "alert" : "status"}>
            <Icon name={ICON[t.kind]} />
            <span className="ui-toast-text">{t.message}</span>
            {t.action && (
              <button type="button" className="ui-toast-action" onClick={() => { t.action!.onClick(); dismiss(t.id); }}>
                {t.action.label}
              </button>
            )}
            <button type="button" className="ui-toast-close" aria-label="Dismiss" onClick={() => dismiss(t.id)}>
              <Icon name="x" size={14} />
            </button>
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

/** Outside a provider this returns no-ops, so a component that toasts can be rendered alone. */
export function useToast(): ToastApi {
  return useContext(Ctx);
}
