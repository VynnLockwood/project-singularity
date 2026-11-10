import type { ReactNode } from "react";

export function Panel({ title, actions, children, as: Tag = "section", className }: {
  title?: ReactNode; actions?: ReactNode; children?: ReactNode; as?: "section" | "div" | "aside"; className?: string;
}) {
  return (
    <Tag className={["ui-panel", className].filter(Boolean).join(" ")}>
      {(title || actions) && (
        <header className="ui-panel-head">
          {title && <h3 className="ui-panel-title">{title}</h3>}
          {actions && <div className="ui-panel-actions">{actions}</div>}
        </header>
      )}
      {children}
    </Tag>
  );
}
