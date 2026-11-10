/** Where the person is in the app. The URL is the only source of truth; this file turns it into a typed value and back. */
export type View = "chat" | "database" | "data" | "duplicates" | "scripts" | "activity" | "connect" | "settings";
export type SettingsSection = "quality" | "rules" | "team";

export type Route =
  | { view: "chat"; conversation?: number }
  | { view: "database" | "data"; schema?: string; table?: string }
  | { view: "settings"; section?: SettingsSection }
  | { view: "duplicates" | "scripts" | "activity" | "connect" }
  | { view: "notfound"; path: string };

const SECTIONS: readonly string[] = ["quality", "rules", "team"];
const PLAIN = ["duplicates", "scripts", "activity", "connect"] as const;

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
    default: return join(route.view);
  }
}
