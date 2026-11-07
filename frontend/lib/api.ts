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
  kind: "schema" | "data" | "config";
  source: string;
  requester: string;
  prompt: string;
  createdAt: string;
  decidedBy: string | null;
  error: string;
  decisionNote: string;
  tables: string[];
  diff: string[];
  sql?: string[];
  check: { violations?: Violation[]; errors?: string[] };
}

export const getQueue = () => call<{ changes: Change[] }>("GET", "/queue").then((r) => r.changes ?? []);
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
export const sendMessage = (id: number, content: string) =>
  call<{ messages: ChatMessage[]; changes: Change[] }>("POST", `/chat/conversations/${id}/messages`, { content });

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
export const getGraph = () => call<SchemaGraph>("GET", "/schema/graph");

// ---- search -------------------------------------------------------------------

export interface SearchHit {
  kind: "table" | "column";
  table: string;
  column: string | null;
  confidence: number;
  description: string;
}
export interface SearchResult {
  results: SearchHit[];
  confident: boolean;
  suggestion: string | null;
}
export const searchSchema = (q: string) => call<SearchResult>("GET", `/search?q=${encodeURIComponent(q)}`);
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
export const getRows = (table: string, q: RowsQuery) => {
  const p = new URLSearchParams({ offset: String(q.offset), limit: String(q.limit) });
  if (q.sort) p.set("sort", q.sort);
  if (q.dir) p.set("dir", q.dir);
  if (q.inactive) p.set("inactive", "1");
  const filters = Object.fromEntries(Object.entries(q.filters ?? {}).filter(([, v]) => v));
  if (Object.keys(filters).length) p.set("filters", JSON.stringify(filters));
  return call<RowsPage>("GET", `/schema/tables/${encodeURIComponent(table)}/rows?${p}`);
};
export interface RefOptions {
  labelColumn: string | null;
  truncated: boolean;
  options: { id: number | string; label: string }[];
}
export const getOptions = (table: string) =>
  call<RefOptions>("GET", `/schema/tables/${encodeURIComponent(table)}/options?limit=200`);

export type Operation = Record<string, unknown>;
export const proposeChange = (operations: Operation[], prompt: string, source: "grid" | "web" = "grid") =>
  call<Change>("POST", "/changes", { operations, source, prompt });

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

export interface Tunable {
  key: string;
  label: string;
  help: string;
  kind: "int" | "float" | "model";
  min: number | null;
  max: number | null;
  default: number | string;
  value: number | string;
  overridden: boolean;
  options?: string[];
}
export const getSettings = () => call<{ settings: Tunable[] }>("GET", "/settings").then((r) => r.settings);
/** value null resets a setting to the singularity.yaml default */
export const saveSettings = (changes: Record<string, number | string | null>) =>
  call<{ settings: Tunable[] }>("PATCH", "/settings", changes).then((r) => r.settings);

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

export const validateScript = (id: number, mapping?: unknown) =>
  call<ScriptReport>("POST", `/scripts/${id}/validate`, mapping ? { mapping } : {});
export const submitScript = (id: number, note = "") =>
  call<{ change_id: number }>("POST", `/scripts/${id}/submit`, { note });
export const listScripts = () => call<{ scripts: ScriptInfo[] }>("GET", "/scripts").then((r) => r.scripts);
export const getScript = (id: number) => call<ScriptInfo>("GET", `/scripts/${id}`);
