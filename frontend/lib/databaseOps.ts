import type { Operation } from "./api";

// Mirrors backend/gate/operations.py for early, friendly feedback. The gate stays the authority.

export const COLUMN_TYPES: string[] = [
  "text", "integer", "bigint", "smallint", "boolean", "date", "timestamptz", "timestamp", "uuid", "jsonb",
  "real", "double precision", "numeric", "numeric(p,s)", "varchar(n)",
];

const NAME_RE = /^[a-z][a-z0-9_]{0,62}$/;
const RESERVED_SCHEMAS = ["singularity", "public", "information_schema"];

export function validateName(name: string, taken: string[]): string | null {
  if (!name) return "Enter a name.";
  if (name.length > 63) return "Use at most 63 characters.";
  if (!NAME_RE.test(name)) {
    return "Use lowercase letters, digits and underscores, starting with a letter (no spaces or capitals).";
  }
  if (taken.includes(name)) return `"${name}" already exists.`;
  return null;
}

export function validateSchemaName(name: string, taken: string[]): string | null {
  const base = validateName(name, []);
  if (base) return base;
  if (RESERVED_SCHEMAS.includes(name) || name.startsWith("pg_")) {
    return `"${name}" is reserved (singularity, public, information_schema and pg_* are not allowed).`;
  }
  if (taken.includes(name)) return `"${name}" already exists.`;
  return null;
}

const PLAIN_TYPES = new Set([
  "text", "integer", "bigint", "smallint", "boolean", "date", "timestamptz", "timestamp", "uuid", "jsonb",
  "real", "double precision", "numeric",
]);

export function validateType(t: string): string | null {
  const n = normalizeType(t);
  if (PLAIN_TYPES.has(n)) return null;
  const num = /^numeric\((\d{1,2}),\s?(\d{1,2})\)$/.exec(n);
  if (num) {
    const p = Number(num[1]);
    const s = Number(num[2]);
    // The gate's pattern is looser; Postgres refuses precision 0 and scale above precision.
    if (p < 1) return "numeric precision must be at least 1.";
    if (s > p) return "numeric scale cannot exceed precision.";
    return null;
  }
  const vc = /^varchar\((\d{1,4})\)$/.exec(n);
  if (vc) return Number(vc[1]) < 1 ? "varchar length must be at least 1." : null;
  if (n === "varchar") return "Give varchar a length, like varchar(40).";
  return `Unsupported type "${t}". Use ${COLUMN_TYPES.join(", ")}.`;
}

const DEFAULT_FUNCS = ["now()", "gen_random_uuid()", "current_date"];
const NUMERIC_TYPES = new Set(["integer", "bigint", "smallint", "real", "double precision", "numeric"]);

export function normalizeType(t: string): string {
  return t.trim().toLowerCase().replace(/\s+/g, " ").replace(", ", ",");
}

export function parseDefault(input: string, type?: string): unknown {
  const s = input.trim();
  if (s === "") return undefined;
  const fn = s.toLowerCase();
  if (DEFAULT_FUNCS.includes(fn)) return fn;
  const t = type === undefined ? "" : normalizeType(type);
  if (NUMERIC_TYPES.has(t) || t.startsWith("numeric(")) {
    const n = Number(s);
    return Number.isFinite(n) && String(n) === s ? n : s;
  }
  if (t === "boolean") {
    const l = s.toLowerCase();
    return l === "true" ? true : l === "false" ? false : s;
  }
  if (t === "jsonb") {
    try {
      return JSON.parse(s);
    } catch {
      return s;
    }
  }
  return s;
}

export interface ColumnSpec {
  name: string;
  type: string;
  nullable?: boolean;
  unique?: boolean;
  default?: string;
  comment?: string;
  references?: { table: string; column?: string };
}

function columnObject(c: ColumnSpec): Record<string, unknown> {
  const out: Record<string, unknown> = { name: c.name, type: normalizeType(c.type) };
  if (c.nullable !== undefined) out.nullable = c.nullable;
  if (c.unique !== undefined) out.unique = c.unique;
  const d = c.default === undefined ? undefined : parseDefault(c.default, c.type);
  if (d !== undefined) out.default = d;
  if (c.comment) out.comment = c.comment;
  if (c.references && c.references.table) {
    out.references = c.references.column
      ? { table: c.references.table, column: c.references.column }
      : { table: c.references.table };
  }
  return out;
}

export const createSchemaOp = (name: string, comment?: string): Operation =>
  comment ? { op: "create_schema", name, comment } : { op: "create_schema", name };
export const renameSchemaOp = (schema: string, newName: string): Operation =>
  ({ op: "rename_schema", schema, new_name: newName });
export const dropSchemaOp = (schema: string, reason: string): Operation => ({ op: "drop_schema", schema, reason });

export const createTableOp = (name: string, columns: ColumnSpec[], comment?: string): Operation => {
  const op: Operation = { op: "create_table", name, columns: columns.map(columnObject) };
  if (comment) op.comment = comment;
  return op;
};
export const addColumnOp = (table: string, column: ColumnSpec): Operation =>
  ({ op: "add_column", table, column: columnObject(column) });
export const renameTableOp = (table: string, newName: string): Operation =>
  ({ op: "rename_table", table, new_name: newName });
export const renameColumnOp = (table: string, column: string, newName: string): Operation =>
  ({ op: "rename_column", table, column, new_name: newName });
export const alterColumnTypeOp = (table: string, column: string, type: string): Operation =>
  ({ op: "alter_column_type", table, column, type: normalizeType(type) });
export const setColumnCommentOp = (table: string, column: string, comment: string): Operation =>
  ({ op: "set_column_comment", table, column, comment });
export const dropColumnOp = (table: string, column: string, reason: string): Operation =>
  ({ op: "drop_column", table, column, reason });
export const dropTableOp = (table: string, reason: string): Operation => ({ op: "drop_table", table, reason });
export const addIndexOp = (table: string, columns: string[], unique?: boolean): Operation =>
  unique ? { op: "add_index", table, columns, unique: true } : { op: "add_index", table, columns };
export const addUniqueOp = (table: string, columns: string[]): Operation => ({ op: "add_unique", table, columns });
export const addForeignKeyOp = (
  table: string, column: string, refTable: string, refColumn?: string, onDelete?: string,
): Operation => {
  const op: Operation = { op: "add_foreign_key", table, column, ref_table: refTable };
  if (refColumn) op.ref_column = refColumn;
  if (onDelete) op.on_delete = onDelete;
  return op;
};
export const dropIndexOp = (name: string, reason: string): Operation => ({ op: "drop_index", name, reason });
export const dropConstraintOp = (table: string, name: string, reason: string): Operation =>
  ({ op: "drop_constraint", table, name, reason });

const DESTRUCTIVE = new Set(["drop_column", "drop_table", "drop_schema", "drop_index", "drop_constraint"]);
export const isDestructive = (op: Operation): boolean => DESTRUCTIVE.has(String(op.op));

export const describeOp = (op: Operation): string => {
  const s = (k: string) => String(op[k] ?? "");
  switch (op.op) {
    case "create_schema": return `Create database ${s("name")}`;
    case "rename_schema": return `Rename database ${s("schema")} to ${s("new_name")}`;
    case "drop_schema": return `Drop database ${s("schema")}`;
    case "create_table": {
      const n = Array.isArray(op.columns) ? op.columns.length : 0;
      return `Create table ${s("name")} (${n} ${n === 1 ? "column" : "columns"})`;
    }
    case "add_column": {
      const c = (op.column ?? {}) as Record<string, unknown>;
      return `Add column ${s("table")}.${String(c.name ?? "")} (${String(c.type ?? "")})`;
    }
    case "rename_table": return `Rename table ${s("table")} to ${s("new_name")}`;
    case "rename_column": return `Rename column ${s("table")}.${s("column")} to ${s("new_name")}`;
    case "alter_column_type": return `Change type of ${s("table")}.${s("column")} to ${s("type")}`;
    case "set_column_comment": return `Set comment on ${s("table")}.${s("column")}`;
    case "drop_column": return `Drop column ${s("table")}.${s("column")}`;
    case "drop_table": return `Drop table ${s("table")}`;
    case "add_index": {
      const cols = Array.isArray(op.columns) ? op.columns.join(", ") : "";
      return `Add ${op.unique ? "unique " : ""}index on ${s("table")} (${cols})`;
    }
    case "add_unique": {
      const cols = Array.isArray(op.columns) ? op.columns.join(", ") : "";
      return `Add unique constraint on ${s("table")} (${cols})`;
    }
    case "add_foreign_key": {
      const ref = op.ref_column ? `${s("ref_table")}.${s("ref_column")}` : s("ref_table");
      return `Add foreign key ${s("table")}.${s("column")} -> ${ref}`;
    }
    case "drop_index": return `Drop index ${s("name")}`;
    case "drop_constraint": return `Drop constraint ${s("table")}.${s("name")}`;
    default: return String(op.op ?? "Unknown operation");
  }
};
