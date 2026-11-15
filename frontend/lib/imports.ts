import { call } from "./api";

export type ImportState =
  | "profiling" | "matching" | "designing" | "checking"
  | "waiting_for_answer" | "ready" | "failed" | "submitted";

/** States where nothing is running on the server, so there is nothing to poll for. */
export const RESTING: readonly ImportState[] = ["ready", "failed", "submitted", "waiting_for_answer"];
export const isRunning = (s: string) => !(RESTING as readonly string[]).includes(s);

export interface ImportMessage {
  role: "profiler" | "matcher" | "designer" | "critic" | "person" | "system";
  text: string;
  details: Record<string, unknown> | null;
  at: string;
}

export interface PlanTable {
  target: string;
  columns: Record<string, string>; // target column -> source header
  drop?: string[];
  add_columns?: { name: string; type: string; nullable?: boolean; comment?: string }[];
  refs?: { column?: string; parent?: string; parent_key?: string; source_column?: string; external?: boolean }[];
}
export interface ImportPlan {
  mapping: { tables: Record<string, PlanTable> };
  filters?: Record<string, unknown[]>;
  reasons?: Record<string, string>;
  questions?: string[];
  picked?: Record<string, number[]>;
  skip?: Record<string, number[]>;
}
export interface MaybeRow { row: number; id: number; score: number; existing: string }
export interface ImportReport {
  ok: boolean;
  problems: string[];
  scriptId?: number | null;
  tables?: Record<string, { target: string; rows: number; new: number; exists: number; filtered: number; skipped: number; maybe?: MaybeRow[] }>;
  badRows?: { line: number; message: string }[];
  dropped?: { sheet: string; column: string; reason: string }[];
  warnings?: string[];
  /** The plan is fine but loads nothing: every row exists, is filtered or skipped, and no table changes. */
  nothing?: boolean;
}
export interface ImportRun {
  id: number;
  schema: string;
  state: ImportState;
  prompt: string;
  fileName: string;
  round: number;
  profile: { sheets?: { name: string; rows?: number; columns?: { header: string; type?: string }[] }[] } | null;
  matches: { sources?: Record<string, { rows?: { table?: string; maybe?: MaybeRow[] } }> } | null;
  plan: ImportPlan | null;
  report: ImportReport | null;
  messages: ImportMessage[];
  changeId: number | null;
  expiresAt: string;
}

export interface MappingEdit {
  mapping: unknown;
  filters: unknown;
  skip: Record<string, number[]>;
}

const withKey = <T extends object>(body: T, apiKey?: string) => (apiKey ? { ...body, apiKey } : body);

export const startImport = (b: { uploadId: number; schema: string; prompt: string; apiKey?: string }) =>
  call<ImportRun>("POST", "/imports", withKey({ uploadId: b.uploadId, schema: b.schema, prompt: b.prompt }, b.apiKey));
export const getImport = (id: number, signal?: AbortSignal) => call<ImportRun>("GET", `/imports/${id}`, undefined, signal);
export const answerImport = (id: number, text: string, apiKey?: string) =>
  call<ImportRun>("POST", `/imports/${id}/answer`, withKey({ text }, apiKey));
export const recheckImport = (id: number, edit: MappingEdit) => call<ImportRun>("POST", `/imports/${id}/recheck`, edit);
export const submitImport = (id: number, note?: string) =>
  call<{ changeId: number }>("POST", `/imports/${id}/submit`, note ? { note } : {});
export const cancelImport = (id: number) => call<unknown>("DELETE", `/imports/${id}`);
