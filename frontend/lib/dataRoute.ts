/** The Data tab's view state (sort, filters, inactive) and how it is written into, and read from, the address. */
export interface DataView {
  sort: string | null;
  dir: "asc" | "desc";
  filters: Record<string, string>;
  inactive: boolean;
}

export const EMPTY_VIEW: DataView = { sort: null, dir: "asc", filters: {}, inactive: false };

/** Reads a query string. Anything unknown or malformed means "none": a hand-edited link never breaks the page. */
export function parseView(search: string): DataView {
  const q = new URLSearchParams(search);
  const sort = q.get("sort") || null;
  const filters: Record<string, string> = {};
  for (const [key, value] of q) {
    if (key.startsWith("f.") && key.length > 2 && value !== "" && !(key.slice(2) in filters)) filters[key.slice(2)] = value;
  }
  return { sort, dir: sort && q.get("dir") === "desc" ? "desc" : "asc", filters, inactive: q.get("inactive") === "1" };
}

export function viewToSearch(view: DataView): string {
  const q = new URLSearchParams();
  if (view.sort) {
    q.set("sort", view.sort);
    if (view.dir === "desc") q.set("dir", "desc");
  }
  if (view.inactive) q.set("inactive", "1");
  for (const [col, text] of Object.entries(view.filters)) if (text !== "") q.set(`f.${col}`, text);
  const s = q.toString();
  return s ? `?${s}` : "";
}
