import type { ReactNode } from "react";
import { Icon, type IconName } from "./Icon";

/** Placeholder rows shown while the first load is in flight. */
export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="ui-skeleton" aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => <div key={i} className="ui-skeleton-row" style={{ width: `${92 - ((i * 17) % 40)}%` }} />)}
    </div>
  );
}

/** A thin bar for the part of the page that is loading. It takes no room when idle. */
export function ProgressBar({ active, label = "Loading" }: { active: boolean; label?: string }) {
  return (
    <div className="ui-progress" data-active={active} {...(active ? { role: "progressbar", "aria-label": label } : {})}>
      {active && <span />}
    </div>
  );
}

export function EmptyState({ icon, title, children, action }: { icon: IconName; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="ui-empty">
      <span className="ui-empty-icon"><Icon name={icon} size={22} /></span>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="ui-kbd">{children}</kbd>;
}
