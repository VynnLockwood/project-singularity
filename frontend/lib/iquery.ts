import { call, getChatApiKey } from "./api";

export interface FoundValue {
  table: string;
  column: string;
  value: string;
}

export interface Suggestion {
  sql: string | null;
  source: "model" | "memory";
  tables: string[];
  values: FoundValue[];
  searchOffline?: boolean;
  lowConfidence?: boolean;
  reason?: string | null;
  requestId: string;
}

export interface SuggestBody {
  schema: string;
  comments: string[];
  sqlContext?: string;
  requestId?: string;
}

export interface QueryResult {
  columns: string[];
  rows: unknown[][];
  truncated: boolean;
}

export interface SavedExample {
  id: number;
  comments: string;
  sql: string;
  status: "candidate" | "active";
  useCount: number;
  createdBy: string | null;
  lastUsed: string | null;
}

const MAX_COMMENTS = 10;
const MAX_CONTEXT = 4000;

export const isCommentLine = (line: string): boolean => /^\s*--/.test(line);

/** The text before `pos`, plus the rest of the line `pos` is on, split into lines. */
function linesUpTo(text: string, pos: number): string[] {
  const end = text.indexOf("\n", pos);
  return text.slice(0, end === -1 ? text.length : end).split("\n");
}

/** `--` comments above the cursor (and on its line), oldest first, empty ones dropped, at most the last 10. */
export function commentsAbove(text: string, pos: number): string[] {
  return linesUpTo(text, pos)
    .filter(isCommentLine)
    .map((l) => l.replace(/^\s*--/, "").trim())
    .filter(Boolean)
    .slice(-MAX_COMMENTS);
}

/** The non-comment text above the cursor, last 4,000 characters. */
export function sqlContextAbove(text: string, pos: number): string {
  return text
    .slice(0, pos)
    .split("\n")
    .filter((l) => !isCommentLine(l))
    .join("\n")
    .trim()
    .slice(-MAX_CONTEXT);
}

/** Statement ranges: split on `;` outside quotes, `$$` bodies and `--` comments. `to` is the index of the `;` (or the end). */
function ranges(text: string): { from: number; to: number }[] {
  const out: { from: number; to: number }[] = [];
  let from = 0;
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (c === "'" || c === '"') {
      i++;
      while (i < text.length) {
        if (text[i] === c) {
          if (text[i + 1] === c) i += 2; // doubled quote stays inside
          else break;
        } else i++;
      }
      i++;
    } else if (c === "-" && text[i + 1] === "-") {
      while (i < text.length && text[i] !== "\n") i++;
    } else if (c === "$" && text[i + 1] === "$") {
      const close = text.indexOf("$$", i + 2);
      i = close === -1 ? text.length : close + 2;
    } else if (c === ";") {
      out.push({ from, to: i });
      from = i + 1;
      i++;
    } else i++;
  }
  out.push({ from, to: text.length });
  return out;
}

/** The statement the cursor is in, with comment lines removed; null when it is blank. */
export function statementAt(text: string, pos: number): { from: number; to: number; sql: string } | null {
  const r = ranges(text).find((x) => pos <= x.to) ?? ranges(text).at(-1)!;
  const sql = text
    .slice(r.from, r.to)
    .split("\n")
    .filter((l) => !isCommentLine(l))
    .join("\n")
    .trim();
  return sql ? { from: r.from, to: r.to, sql } : null;
}

const ROOT = "/intelligent-query";
type WithKey<T> = T & { apiKey: string };
const withKey = <T extends object>(body: T): WithKey<T> => ({ ...body, apiKey: getChatApiKey() });

export const recallQuery = (body: SuggestBody, signal?: AbortSignal) =>
  call<Suggestion>("POST", `${ROOT}/recall`, withKey(body), signal);

export const suggestQuery = (body: SuggestBody, signal?: AbortSignal) =>
  call<Suggestion>("POST", `${ROOT}/suggest`, withKey(body), signal);

export const learnQuery = (body: { schema: string; comments: string[]; sql: string; values: FoundValue[] }) =>
  call<{ id: number; status: string }>("POST", `${ROOT}/learn`, body);

export const runQuery = (schema: string, sql: string) => call<QueryResult>("POST", "/query", { schema, sql });

export const listExamples = (schema: string) =>
  call<{ examples: SavedExample[] }>("GET", `${ROOT}/examples?schema=${encodeURIComponent(schema)}`).then((r) => r.examples);

export const deleteExample = (id: number) => call<unknown>("DELETE", `${ROOT}/examples/${id}`);
