/** Where the person is in the app. The URL is the only source of truth; this file turns it into a typed value and back. */
export type View = "chat" | "database" | "data" | "duplicates" | "scripts" | "activity" | "connect" | "settings";
export type SettingsSection = "quality" | "rules" | "team";

export type Route =
  | { view: "chat"; conversation?: number }
  | { view: "database" | "data"; schema?: string; table?: string }
  | { view: "settings"; section?: SettingsSection }
  | { view: "activity"; tab?: "history" }
  | { view: "duplicates" | "scripts" | "connect" }
  | { view: "notfound"; path: string };

const SECTIONS: readonly string[] = ["quality", "rules", "team"];
const PLAIN = ["duplicates", "scripts", "connect"] as const;

export function parseRoute(pathname: string): Route {
  let parts: string[];
  try {
    parts = pathname.split("/").filter(Boolean).map(decodeURIComponent);
  } catch {
    return { view: "notfound", path: pathname }; // a broken %-escape is someone's mangled link, not a crash
  }
  const [head, a, b] = parts;
  if (head === undefined) return { view: "chat" };
  if (head === "chat") {
    const id = a !== undefined && /^\d+$/.test(a) ? Number(a) : undefined;
    return id === undefined ? { view: "chat" } : { view: "chat", conversation: id };
  }
  if (head === "database" || head === "data") {
    return { view: head, ...(a ? { schema: a } : {}), ...(a && b ? { table: b } : {}) };
  }
  if (head === "settings") {
    return a && SECTIONS.includes(a) ? { view: "settings", section: a as SettingsSection } : { view: "settings" };
  }
  if (head === "activity") return a === "history" ? { view: "activity", tab: "history" } : { view: "activity" };
  if (head === "queue") return { view: "activity" }; // the tab's name before the rework
  if ((PLAIN as readonly string[]).includes(head)) return { view: head as (typeof PLAIN)[number] };
  return { view: "notfound", path: pathname };
}

const join = (...parts: (string | number | undefined)[]) =>
  "/" + parts.filter((p) => p !== undefined && p !== "").map((p) => encodeURIComponent(String(p))).join("/");

export function buildPath(route: Route): string {
  switch (route.view) {
    case "notfound": return route.path;
    case "chat": return route.conversation === undefined ? "/" : join("chat", route.conversation);
    case "database":
    case "data": return join(route.view, route.schema, route.schema ? route.table : undefined);
    case "settings": return join("settings", route.section);
    case "activity": return join("activity", route.tab);
    default: return join(route.view);
  }
}

/**
 * True when a path asks for a file (`/engine/x.js`, `/favicon.ico`) rather than a place in the app.
 * The catch-all page answers those with a real 404, so a missing optional file (the WebAssembly
 * engine) is reported as missing instead of being served the app's HTML.
 */
export function isAssetPath(segments: string[] | undefined): boolean {
  const last = segments?.[segments.length - 1];
  return last !== undefined && /\.[a-z0-9]{2,5}$/i.test(last);
}
