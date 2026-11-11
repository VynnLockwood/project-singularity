export type Role = "viewer" | "editor" | "approver";

export interface Me {
  id: number;
  email: string;
  name: string;
  role: Role;
}

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api";

/** Returns the signed-in user, or null when not signed in. */
export async function getMe(): Promise<Me | null> {
  const res = await fetch(`${BASE}/me`, { credentials: "same-origin" });
  if (res.status === 401) return null;
  if (!res.ok) throw new Error(`GET /me failed: ${res.status}`);
  return (await res.json()) as Me;
}

export const googleLoginUrl = `${BASE}/auth/google/login`;

/** Link for the SQL export; a plain href lets the browser stream the download. */
export function exportUrl(schema: string, data: boolean): string {
  return `${BASE}/schema/export?schema=${encodeURIComponent(schema)}&data=${data ? 1 : 0}`;
}

function csrfToken(): string {
  const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

export async function logout(): Promise<void> {
  await fetch(`${BASE}/csrf`, { credentials: "same-origin" }); // makes sure the CSRF cookie exists
  await fetch(`${BASE}/auth/logout`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRFToken": csrfToken() },
  });
}

// ---- generic requests ---------------------------------------------------------

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string) {
    super(message);
  }
}

async function call<T>(method: "GET" | "POST" | "PATCH" | "DELETE", path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== "GET") {
    if (!csrfToken()) await fetch(`${BASE}/csrf`, { credentials: "same-origin" });
    headers["X-CSRFToken"] = csrfToken();
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(`${BASE}${path}`, {
    method,
    credentials: "same-origin",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, Array.isArray(data.errors) && data.errors.length ? data.errors.join("; ") : (data.error ?? `request failed (${res.status})`), data.code);
  return data as T;
}

/** Master is the default schema: its URLs carry no `schema=` param, any other schema adds one. */
const DEFAULT_SCHEMA = "master";
const schemaParam = (schema: string) => (schema === DEFAULT_SCHEMA ? "" : `schema=${encodeURIComponent(schema)}`);
/** Appends the schema param to a path, picking `?` or `&` as needed. */
function withSchema(path: string, schema: string): string {
  const sp = schemaParam(schema);
  if (!sp) return path;
  return `${path}${path.includes("?") ? "&" : "?"}${sp}`;
}

// ---- changes ------------------------------------------------------------------

export type ChangeStatus = "pending" | "rejected" | "applied" | "failed" | "stale";

export interface Violation {
  severity: "block" | "warn";
  message: string;
  suggestion?: string;
}

export interface Change {
  id: number;
  status: ChangeStatus;
  kind: "schema" | "data" | "config" | "sql";
  source: string;
  requester: string;
  prompt: string;
  createdAt: string;
  decidedBy: string | null;
  error: string;
  decisionNote: string;
  tables: string[];
  /** schema the change targets; absent means master */
  schema?: string;
  diff: string[];
  sql?: string[];
  /** the stored operations; a raw SQL change holds one {op: "raw_sql", sql, reason} */
  operations?: { op: string; sql?: string; reason?: string }[];
  check: { violations?: Violation[]; errors?: string[] };
  decidedAt?: string | null;
  appliedAt?: string | null;
  /** applied changes only: whether the server can undo it automatically */
  reversible?: boolean | null;
  /** the id of the change this one reverts */
  reverts?: number | null;
}

export const getQueue = () => call<{ changes: Change[] }>("GET", "/queue").then((r) => r.changes ?? []);
export interface ChangeFilters { status?: ChangeStatus; kind?: Change["kind"]; schema?: string; mine?: boolean }
export const listChanges = (f: ChangeFilters, limit: number) => {
  const q = new URLSearchParams();
  if (f.status) q.set("status", f.status);
  if (f.kind) q.set("kind", f.kind);
  if (f.schema) q.set("schema", f.schema);
  if (f.mine) q.set("mine", "1");
  q.set("limit", String(limit));
  return call<{ changes: Change[]; more?: boolean }>("GET", `/changes?${q}`).then((r) => ({ changes: r.changes ?? [], more: r.more === true }));
};
/** One change with its operations and SQL (the list leaves them out). */
export const getChange = (id: number) => call<Change>("GET", `/changes/${id}`);
export const revertChange = (id: number) => call<Change>("POST", `/changes/${id}/revert`, {});
export const decide = (id: number, action: "approve" | "reject", note = "") =>
  call<Change>("POST", `/changes/${id}/${action}`, note ? { note } : {});

// ---- chat ---------------------------------------------------------------------

export interface ChatMessage {
  id: number;
  role: "user" | "assistant" | "tool";
  content: string;
  toolCalls?: string[];
  toolName?: string;
}

export interface ConversationSummary {
  id: number;
  title: string;
  model: string;
  updatedAt: string;
}

export interface ConversationDetail extends ConversationSummary {
  messages: ChatMessage[];
  changes: Change[];
}

export const listConversations = () =>
  call<{ conversations: ConversationSummary[] }>("GET", "/chat/conversations").then((r) => r.conversations);
export const createConversation = () => call<ConversationSummary>("POST", "/chat/conversations", {});
export const getConversation = (id: number) => call<ConversationDetail>("GET", `/chat/conversations/${id}`);

/**
 * The user's own provider key, kept in this browser only. Sent per request and never
 * stored server-side; empty string means "use the server's configured key".
 */
const KEY_STORAGE = "singularity.chatApiKey";

export function getChatApiKey(): string {
  try {
    return window.localStorage.getItem(KEY_STORAGE) ?? "";
  } catch {
    return "";
  }
}

export function setChatApiKey(key: string): void {
  try {
    if (key) window.localStorage.setItem(KEY_STORAGE, key);
    else window.localStorage.removeItem(KEY_STORAGE);
  } catch {
    /* storage unavailable (private mode): the key just won't persist */
  }
}

export const sendMessage = (id: number, content: string) =>
  call<{ messages: ChatMessage[]; changes: Change[] }>("POST", `/chat/conversations/${id}/messages`, {
    content,
    apiKey: getChatApiKey(),
  });

// ---- schema -------------------------------------------------------------------

export interface GraphColumn {
  name: string;
  type: string;
  nullable: boolean;
  primaryKey: boolean;
}
export interface GraphTable {
  name: string;
  comment: string | null;
  rowEstimate: number;
  columns: GraphColumn[];
}
export interface Relation {
  from: { table: string; column: string };
  to: { table: string; column: string };
}
export interface SchemaGraph {
  tables: GraphTable[];
  relations: Relation[];
}
export const getGraph = (schema: string = DEFAULT_SCHEMA) => call<SchemaGraph>("GET", withSchema("/schema/graph", schema));

export interface SchemaInfo {
  name: string;
  tables: number;
  comment: string | null;
}
export const getSchemas = () => call<{ schemas: SchemaInfo[] }>("GET", "/schema/schemas").then((r) => r.schemas);

export interface TableDetailColumn {
  name: string;
  type: string;
  nullable: boolean;
  default: string | null;
  comment: string | null;
  identity: boolean;
}
export interface TableDetail {
  name: string;
  comment: string | null;
  columns: TableDetailColumn[];
  constraints: { name: string; kind: string; definition: string }[];
  indexes: { name: string; definition: string }[];
  /** audit columns the server hides by default */
  hiddenColumns: string[];
}
export const getTable = (name: string, schema: string = DEFAULT_SCHEMA) =>
  call<TableDetail>("GET", withSchema(`/schema/tables/${encodeURIComponent(name)}`, schema));

// ---- search -------------------------------------------------------------------

export interface SearchHit {
  kind: "table" | "column";
  table: string;
  /** schema the hit belongs to; absent means master */
  schema?: string;
  column: string | null;
  confidence: number;
  description: string;
}
export interface SearchResult {
  results: SearchHit[];
  confident: boolean;
  suggestion: string | null;
}
export const searchSchema = (q: string, schema: string = DEFAULT_SCHEMA) =>
  call<SearchResult>("GET", withSchema(`/search?q=${encodeURIComponent(q)}`, schema));
export const reindexSearch = (rebuild = false) =>
  call<{ embedded: number; unchanged: number; removed: number }>("POST", "/search/reindex", { rebuild });

// ---- grid ---------------------------------------------------------------------

export interface GridColumn {
  name: string;
  type: string;
  nullable: boolean;
  readOnly: boolean;
  references: { table: string; column: string } | null;
}
export type Row = Record<string, unknown>;
export interface RowsPage {
  table: string;
  total: number;
  offset: number;
  limit: number;
  /** name of the table's primary key column (e.g. "province_id"); older servers send none, meaning "id" */
  primaryKey?: string | null;
  columns: GridColumn[];
  rows: Row[];
}
export interface RowsQuery {
  offset: number;
  limit: number;
  sort?: string;
  dir?: "asc" | "desc";
  filters?: Record<string, string>;
  inactive?: boolean;
}
export const getRows = (table: string, q: RowsQuery, schema: string = DEFAULT_SCHEMA) => {
  const p = new URLSearchParams({ offset: String(q.offset), limit: String(q.limit) });
  if (q.sort) p.set("sort", q.sort);
  if (q.dir) p.set("dir", q.dir);
  if (q.inactive) p.set("inactive", "1");
  if (schema !== DEFAULT_SCHEMA) p.set("schema", schema);
  const filters = Object.fromEntries(Object.entries(q.filters ?? {}).filter(([, v]) => v));
  if (Object.keys(filters).length) p.set("filters", JSON.stringify(filters));
  return call<RowsPage>("GET", `/schema/tables/${encodeURIComponent(table)}/rows?${p}`);
};
export interface RefOptions {
  labelColumn: string | null;
  truncated: boolean;
  options: { id: number | string; label: string }[];
}
export const getOptions = (table: string, schema: string = DEFAULT_SCHEMA) =>
  call<RefOptions>("GET", withSchema(`/schema/tables/${encodeURIComponent(table)}/options?limit=200`, schema));

export type Operation = Record<string, unknown>;
export const proposeChange = (operations: Operation[], prompt: string, source: "grid" | "web" = "grid", schema: string = DEFAULT_SCHEMA) =>
  call<Change>("POST", "/changes", schema === DEFAULT_SCHEMA ? { operations, source, prompt } : { operations, source, prompt, schema });

// ---- duplicates ---------------------------------------------------------------

export type GroupStatus = "open" | "dismissed" | "merge_pending" | "merged";
export interface DuplicateSummary {
  id: number;
  table: string;
  fields: string[];
  memberIds: number[];
  score: number;
  status: GroupStatus;
  detectedAt: string;
  mergeChange: number | null;
}
export interface DuplicateDetail extends DuplicateSummary {
  /** primary key column of the table; older servers send none, meaning "id" */
  primaryKey?: string;
  /** audit columns the server left out of members (older servers send none) */
  hiddenColumns?: string[];
  members: Record<string, unknown>[];
  referencedBy: { table: string; column: string }[];
}
export interface ScanResult {
  table: string;
  scanned: number;
  truncated: boolean;
  groups: number;
  new: number;
  removed: number;
  fields: string[];
}
export const listDuplicates = (table?: string) =>
  call<{ groups: DuplicateSummary[]; tables: Record<string, number> }>(
    "GET", `/duplicates${table ? `?table=${encodeURIComponent(table)}` : ""}`);
export const getDuplicate = (id: number) => call<DuplicateDetail>("GET", `/duplicates/${id}`);
export const scanDuplicates = (table: string, threshold?: number) =>
  call<ScanResult>("POST", "/duplicates/scan", threshold === undefined ? { table } : { table, threshold });
export const dismissDuplicate = (id: number) => call<DuplicateSummary>("POST", `/duplicates/${id}/dismiss`, {});
export const reopenDuplicate = (id: number) => call<DuplicateSummary>("POST", `/duplicates/${id}/reopen`, {});
export const mergeDuplicate = (id: number, survivorId: number) =>
  call<{ group: DuplicateSummary; change: Change }>("POST", `/duplicates/${id}/merge`, { survivorId });

// ---- MCP connection tokens ----------------------------------------------------

export interface McpToken {
  id: number;
  name: string;
  prefix: string;
  createdAt: string;
  lastUsedAt: string | null;
}
export const listTokens = () => call<{ tokens: McpToken[] }>("GET", "/mcp-tokens").then((r) => r.tokens);
export const createToken = (name: string) =>
  call<{ id: number; name: string; prefix: string; token: string }>("POST", "/mcp-tokens", { name });
export const revokeToken = (id: number) => call<{ ok: boolean }>("DELETE", `/mcp-tokens/${id}`);

// ---- settings: quality knobs, rules, team -------------------------------------

/** Same pattern as the server (settings_app/tunables.py) for a chat model id. */
export const CHAT_MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,119}$/;
export const CHAT_MODELS_MAX = 50;
export interface Tunable {
  key: string;
  label: string;
  help: string;
  kind: "int" | "float" | "model" | "choice" | "models" | "bool";
  min: number | null;
  max: number | null;
  default: number | string | boolean | string[];
  value: number | string | boolean | string[];
  overridden: boolean;
  options?: string[];
}
/** `keySet` says which chat providers have an API key in the server environment — booleans only, never the value. */
export interface SettingsPayload {
  settings: Tunable[];
  keySet: Record<string, boolean>;
}
export const getSettings = () => call<SettingsPayload>("GET", "/settings");
/** value null resets a setting to the singularity.yaml default */
export const saveSettings = (changes: Record<string, number | string | boolean | string[] | null>) =>
  call<SettingsPayload>("PATCH", "/settings", changes);

export interface Rule {
  id: number;
  name: string;
  type: string;
  params: Record<string, unknown>;
  severity: "block" | "warn";
  enabled: boolean;
  description: string;
  version: number;
}
export interface RuleVersion {
  version: number;
  action: string;
  changedBy: string | null;
  createdAt: string;
}
export const listRules = () => call<{ rules: Rule[] }>("GET", "/rules").then((r) => r.rules);
export const updateRule = (id: number, fields: Partial<Pick<Rule, "params" | "severity" | "enabled" | "description">>) =>
  call<Rule>("PATCH", `/rules/${id}`, fields);
export const ruleVersions = (id: number) => call<{ versions: RuleVersion[] }>("GET", `/rules/${id}/versions`).then((r) => r.versions);
export const rollbackRule = (id: number, version: number) => call<Rule>("POST", `/rules/${id}/rollback`, { version });

export const listUsers = () => call<{ users: Me[] }>("GET", "/users").then((r) => r.users);
export const setUserRole = (id: number, role: Me["role"]) => call<Me>("PATCH", `/users/${id}/role`, { role });

// ---- scripts: pg_dump-style SQL intake (editor only) ---------------------------

export interface ScriptReport {
  ok: boolean;
  script_id: number;
  statements: number;
  mapping_used: string | null;
  errors: { kind: string; line: number; statement: string; message: string }[];
  errors_truncated?: number;
  ignored: { line: number; statement: string }[];
  ops_summary?: { total: number; by_op: Record<string, number>; tables: string[] };
  gate?: { errors: string[]; truncated?: number };
  tables?: { name: string; source: string; rows: number; orphans: number; sample_orphans: { line: number; row: number; column: string; value: string; parent: string }[] }[];
  dry_run?: { ok: boolean; skipped: boolean; error: string | null; rows_loaded: Record<string, number> };
}

export interface ScriptInfo {
  id: number;
  name: string;
  sha256?: string;
  size: number;
  status: string;
  change_id: number | null;
  created_at?: string;
  expires_at?: string;
  report?: ScriptReport | null;
}

/** multipart upload — call() is JSON-only, so hit fetch directly. */
export async function uploadScript(file: File): Promise<{ script_id: number; sha256: string; size: number }> {
  if (!csrfToken()) await fetch(`${BASE}/csrf`, { credentials: "same-origin" });
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch(`${BASE}/scripts`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRFToken": csrfToken() },
    body: fd,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, data.error ?? `upload failed (${res.status})`);
  return data;
}

/** Paste path: the file picker isn't the only way in — send SQL text directly. */
export const pasteScript = (name: string, content: string) =>
  call<{ script_id: number; sha256: string; size: number }>("POST", "/scripts", { name, content });

export const validateScript = (id: number, mapping?: unknown) =>
  call<ScriptReport>("POST", `/scripts/${id}/validate`, mapping ? { mapping } : {});
export const submitScript = (id: number, note = "") =>
  call<{ change_id: number }>("POST", `/scripts/${id}/submit`, { note });
export const listScripts = () => call<{ scripts: ScriptInfo[] }>("GET", "/scripts").then((r) => r.scripts);
export const getScript = (id: number) => call<ScriptInfo>("GET", `/scripts/${id}`);
