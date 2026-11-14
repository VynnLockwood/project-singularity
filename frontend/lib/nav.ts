import type { Role } from "./api";
import type { IconName } from "@/components/ui";
import type { Route, View } from "./route";

export interface NavItem { view: View; label: string; icon: IconName; minRole: Role; /** stays in the phone bottom bar */ primary?: boolean }
export interface NavGroup { group: "Work" | "Quality" | "Review" | "System"; items: NavItem[] }

const RANK: Record<Role, number> = { viewer: 0, editor: 1, approver: 2 };

export const NAV: NavGroup[] = [
  { group: "Work", items: [
    { view: "chat", label: "Chat", icon: "chat", minRole: "viewer", primary: true },
    { view: "database", label: "Database", icon: "database", minRole: "viewer", primary: true },
    { view: "data", label: "Data", icon: "table", minRole: "viewer", primary: true },
    { view: "query", label: "Intelligent query", icon: "code", minRole: "viewer" },
  ] },
  { group: "Quality", items: [
    { view: "duplicates", label: "Duplicates", icon: "copy", minRole: "viewer" },
    { view: "scripts", label: "Scripts", icon: "script", minRole: "editor" },
  ] },
  { group: "Review", items: [
    { view: "activity", label: "Activity", icon: "activity", minRole: "viewer", primary: true },
  ] },
  { group: "System", items: [
    { view: "connect", label: "Connect", icon: "plug", minRole: "viewer" },
    { view: "settings", label: "Settings", icon: "settings", minRole: "viewer" },
  ] },
];

/** The navigation this role sees. Groups that end up empty are dropped. */
export function navFor(role: Role): NavGroup[] {
  return NAV.map((g) => ({ ...g, items: g.items.filter((i) => RANK[role] >= RANK[i.minRole]) })).filter((g) => g.items.length > 0);
}

export function viewLabel(view: View): string {
  return NAV.flatMap((g) => g.items).find((i) => i.view === view)?.label ?? view;
}

const SECTION_LABEL = { quality: "Quality", rules: "Rules", team: "Team" } as const;

/** The trail shown in the top bar: the view, then where in it. */
export function breadcrumb(route: Route): string[] {
  if (route.view === "notfound") return ["Page not found"];
  const head = viewLabel(route.view);
  if (route.view === "query") return [head, ...(route.schema ? [route.schema] : [])];
  if (route.view === "database" || route.view === "data") return [head, ...(route.schema ? [route.schema] : []), ...(route.schema && route.table ? [route.table] : [])];
  if (route.view === "settings" && route.section) return [head, SECTION_LABEL[route.section]];
  return [head];
}
