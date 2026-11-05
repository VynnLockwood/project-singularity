import type { GridColumn, Operation, Row } from "./api";

export type Coerced = { ok: true; value: unknown } | { ok: false; error: string };

const INT = /^-?\d+$/;
const DEC = /^-?\d+(\.\d+)?$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Turn what was typed into the value the server expects, or say what is wrong. */
export function coerce(col: Pick<GridColumn, "type" | "nullable">, raw: string): Coerced {
  const t = col.type;
  const isText = t === "text" || t.startsWith("character varying");
  if (raw === "") {
    if (col.nullable) return { ok: true, value: null };
    return isText ? { ok: true, value: "" } : { ok: false, error: "required" };
  }
  if (t === "integer" || t === "smallint" || t === "bigint") {
    if (!INT.test(raw)) return { ok: false, error: "whole number" };
    const n = Number(raw);
    return { ok: true, value: Number.isSafeInteger(n) ? n : raw };
  }
  if (t.startsWith("numeric")) return DEC.test(raw) ? { ok: true, value: raw } : { ok: false, error: "number" };
  if (t === "real" || t === "double precision") {
    return DEC.test(raw) ? { ok: true, value: Number(raw) } : { ok: false, error: "number" };
  }
  if (t === "boolean") {
    if (raw === "true" || raw === "false") return { ok: true, value: raw === "true" };
    return { ok: false, error: "true or false" };
  }
  if (t === "date") return /^\d{4}-\d{2}-\d{2}$/.test(raw) && !Number.isNaN(Date.parse(raw)) ? { ok: true, value: raw } : { ok: false, error: "date YYYY-MM-DD" };
  if (t.startsWith("timestamp")) return !Number.isNaN(Date.parse(raw)) ? { ok: true, value: raw } : { ok: false, error: "date and time" };
  if (t === "uuid") return UUID.test(raw) ? { ok: true, value: raw } : { ok: false, error: "uuid" };
  if (t === "jsonb") {
    try {
      return { ok: true, value: JSON.parse(raw) };
    } catch {
      return { ok: false, error: "valid JSON" };
    }
  }
  return { ok: true, value: raw };
}

export const show = (v: unknown): string => (v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));

export interface Draft {
  /** typed text per existing row id and column, only for cells the user changed */
  edits: Record<string, Record<string, string>>;
  /** new rows: typed text per column */
  added: Record<string, string>[];
  /** ids marked for removal (they are deactivated, not deleted) */
  removed: number[];
}

export const emptyDraft = (): Draft => ({ edits: {}, added: [], removed: [] });

export const draftSize = (d: Draft) =>
  Object.values(d.edits).reduce((n, e) => n + Object.keys(e).length, 0) + d.added.length + d.removed.length;

/** Cell problems keyed "id:col" (existing rows) or "new<i>:col" (new rows). */
export function validate(columns: GridColumn[], d: Draft): Record<string, string> {
  const byName = Object.fromEntries(columns.map((c) => [c.name, c]));
  const problems: Record<string, string> = {};
  for (const [id, cells] of Object.entries(d.edits)) {
    if (d.removed.includes(Number(id))) continue;
    for (const [name, raw] of Object.entries(cells)) {
      const r = coerce(byName[name], raw);
      if (!r.ok) problems[`${id}:${name}`] = r.error;
    }
  }
  d.added.forEach((row, i) => {
    for (const c of columns) {
      if (c.readOnly) continue;
      const raw = row[c.name];
      if (raw === undefined || raw === "") {
        if (!c.nullable && c.type !== "text") continue; // a default or the server's check will speak up
        continue;
      }
      const r = coerce(c, raw);
      if (!r.ok) problems[`new${i}:${c.name}`] = r.error;
    }
  });
  return problems;
}

/** The operations a Save sends: edits grouped by identical change, one insert, one deactivate. */
export function buildOperations(table: string, columns: GridColumn[], d: Draft): Operation[] {
  const byName = Object.fromEntries(columns.map((c) => [c.name, c]));
  const ops: Operation[] = [];

  const groups = new Map<string, { set: Record<string, unknown>; ids: number[] }>();
  for (const [id, cells] of Object.entries(d.edits)) {
    if (d.removed.includes(Number(id))) continue;
    const set: Record<string, unknown> = {};
    for (const [name, raw] of Object.entries(cells)) {
      const r = coerce(byName[name], raw);
      if (r.ok) set[name] = r.value;
    }
    if (!Object.keys(set).length) continue;
    const key = JSON.stringify(Object.entries(set).sort());
    const g = groups.get(key) ?? { set, ids: [] };
    g.ids.push(Number(id));
    groups.set(key, g);
  }
  for (const { set, ids } of groups.values()) {
    ops.push({ op: "update_rows", table, where: { id: ids.length === 1 ? ids[0] : ids }, set });
  }

  const rows = d.added
    .map((row) => {
      const out: Record<string, unknown> = {};
      for (const c of columns) {
        const raw = row[c.name];
        if (c.readOnly || raw === undefined || raw === "") continue; // untouched: let the default apply
        const r = coerce(c, raw);
        if (r.ok) out[c.name] = r.value;
      }
      return out;
    })
    .filter((r) => Object.keys(r).length);
  if (rows.length) ops.push({ op: "insert_rows", table, rows });

  if (d.removed.length) {
    ops.push({ op: "deactivate_rows", table, where: { id: d.removed.length === 1 ? d.removed[0] : d.removed } });
  }
  return ops;
}

export function cellText(row: Row, name: string, d: Draft): string {
  const edited = row.id !== undefined ? d.edits[String(row.id)]?.[name] : undefined;
  return edited !== undefined ? edited : show(row[name]);
}
