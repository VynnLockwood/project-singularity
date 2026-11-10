"use client";

import { useEffect, useState } from "react";
import { breadcrumb } from "@/lib/nav";
import type { Route } from "@/lib/route";
import { Icon, Kbd } from "@/components/ui";

interface Props {
  route: Route;
  waiting: number;
  onOpenTray: () => void;
  onOpenPalette?: () => void;
}

/** Where you are, the way to anywhere else, and what is waiting for you. */
export function TopBar({ route, waiting, onOpenTray, onOpenPalette }: Props) {
  const trail = breadcrumb(route);
  // The hint names the real key. Decided after mount so server and client render the same first.
  const [mac, setMac] = useState(false);
  useEffect(() => { setMac(/Mac|iPhone|iPad/.test(navigator.platform)); }, []);
  return (
    <header className="shell-top">
      <ol className="shell-crumbs" aria-label="You are here">
        {trail.map((part, i) => (
          <li key={i} aria-current={i === trail.length - 1 ? "location" : undefined}>{part}</li>
        ))}
      </ol>
      {onOpenPalette && (
        <button type="button" className="shell-search" onClick={onOpenPalette}>
          <Icon name="search" />
          <span>Search or jump to</span>
          <Kbd>{mac ? "⌘ K" : "Ctrl K"}</Kbd>
        </button>
      )}
      <button
        type="button" className="shell-review" data-waiting={waiting > 0}
        aria-label={waiting > 0 ? `Open review, ${waiting} waiting` : "Open review"} onClick={onOpenTray}
      >
        <Icon name="inbox" />
        <span>Review</span>
        {waiting > 0 && <span className="shell-nav-count" aria-hidden="true">{waiting}</span>}
      </button>
    </header>
  );
}
