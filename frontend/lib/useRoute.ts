"use client";

import { useCallback, useEffect, useState } from "react";
import { buildPath, parseRoute, type Route } from "./route";

export type Navigate = (next: Route, opts?: { replace?: boolean }) => void;

/** The current route and a way to change it. Uses the History API directly, so it works without a router in tests. */
export function useRoute(): [Route, Navigate] {
  const [route, setRoute] = useState<Route>(() => parseRoute(typeof location === "undefined" ? "/" : location.pathname));

  useEffect(() => {
    const sync = () => setRoute(parseRoute(location.pathname));
    sync(); // the server renders "/" for every path; pick up the real one after hydration
    addEventListener("popstate", sync);
    return () => removeEventListener("popstate", sync);
  }, []);

  const navigate = useCallback<Navigate>((next, opts) => {
    const path = buildPath(next);
    if (path !== location.pathname) {
      if (opts?.replace) history.replaceState(null, "", path);
      else history.pushState(null, "", path);
    }
    setRoute(parseRoute(path));
  }, []);

  return [route, navigate];
}
