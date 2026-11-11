/** The Database tab's section, kept in the address as `?tab=`. */
export type DbTab = "fields" | "relations" | "erd" | "overview";

const TABS: readonly string[] = ["fields", "relations", "erd", "overview"];
const needsTable: readonly string[] = ["fields", "relations"];

const fallback = (hasTable: boolean): DbTab => (hasTable ? "fields" : "overview");

/** Unknown values, and sections that need a table when there is none, mean the default. */
export function parseTab(search: string, hasTable: boolean): DbTab {
  const tab = new URLSearchParams(search).get("tab") ?? "";
  if (!TABS.includes(tab) || (!hasTable && needsTable.includes(tab))) return fallback(hasTable);
  return tab as DbTab;
}

export function tabToSearch(tab: DbTab, hasTable: boolean): string {
  return tab === fallback(hasTable) ? "" : `?tab=${tab}`;
}
