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

async function call<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
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
  if (!res.ok) throw new ApiError(res.status, data.error ?? `request failed (${res.status})`, data.code);
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
  kind: "schema" | "data";
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

export const getQueue = () => call<{ pending: Change[] }>("GET", "/queue").then((r) => r.pending);
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
