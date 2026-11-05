import type { Change } from "./api";

export const change = (over: Partial<Change> = {}): Change => ({
  id: 1, status: "pending", kind: "schema", source: "web", requester: "ed@example.com", prompt: "",
  createdAt: "2026-11-05T00:00:00Z", decidedBy: null, error: "", decisionNote: "", tables: ["customer"],
  diff: ["+ create table customer"], sql: ["CREATE TABLE ..."], check: {}, ...over,
});
