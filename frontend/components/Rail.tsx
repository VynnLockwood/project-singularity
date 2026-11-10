"use client";

import { useEffect, useState } from "react";
import type { Me } from "@/lib/api";
import { navFor } from "@/lib/nav";
import type { Route } from "@/lib/route";
import type { Navigate } from "@/lib/useRoute";
import { Icon } from "@/components/ui";

interface Props {
  me: Me;
  route: Route;
  navigate: Navigate;
  /** Changes waiting for this person's decision. */
  waiting: number;
  online: boolean;
  onLogout: () => void;
}

/** The main navigation. A side rail on wide screens, icons only on medium ones, a bottom bar with a More sheet on a phone. */
export function Rail({ me, route, navigate, waiting, online, onLogout }: Props) {
  const [more, setMore] = useState(false);
  useEffect(() => { setMore(false); }, [route]);

  return (
    <nav className="shell-rail" aria-label="Main" data-more={more ? "open" : "closed"}>
      <div className="shell-brand">
        <span className="shell-brand-mark" aria-hidden="true" />
        <span className="shell-brand-name">Singularity</span>
      </div>

      {navFor(me.role).map((g) => (
        <div key={g.group} className="shell-nav-group" role="group" aria-label={g.group}>
          <span className="shell-nav-heading" aria-hidden="true">{g.group}</span>
          {g.items.map((item) => {
            const count = item.view === "activity" && waiting > 0 ? waiting : 0;
            return (
              <button
                key={item.view} type="button" className="shell-nav-item" title={item.label}
                data-primary={item.primary ? "true" : undefined}
                aria-current={route.view === item.view ? "page" : undefined}
                aria-describedby={count ? "shell-waiting" : undefined}
                onClick={() => navigate({ view: item.view })}
              >
                <Icon name={item.icon} size={18} />
                <span className="shell-nav-label">{item.label}</span>
                {count > 0 && <span key={count} className="shell-nav-count" aria-hidden="true">{count}</span>}
              </button>
            );
          })}
        </div>
      ))}
      {waiting > 0 && <span id="shell-waiting" className="sr-only">{waiting} waiting</span>}

      <div className="shell-rail-foot">
        <span className="shell-live" data-online={online} title={online ? "Live updates on" : "Trying to reconnect"}>
          <Icon name="dot" size={12} />
          {online ? "Live" : "Reconnecting…"}
        </span>
        <span className="shell-me" title={me.email}>{me.email}</span>
        <span className="shell-role">{me.role}</span>
        <button type="button" className="shell-nav-item shell-signout" onClick={onLogout}>
          <Icon name="logout" size={18} />
          <span className="shell-nav-label">Sign out</span>
        </button>
      </div>

      <button type="button" className="shell-nav-item shell-more" aria-expanded={more} onClick={() => setMore((m) => !m)}>
        <Icon name={more ? "x" : "more"} size={18} />
        <span className="shell-nav-label">{more ? "Close" : "More"}</span>
      </button>
    </nav>
  );
}
