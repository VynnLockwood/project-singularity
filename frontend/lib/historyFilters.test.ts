import { describe, expect, it } from "vitest";
import type { Change, Me } from "./api";
import { DEFAULT_FILTERS, filtersToSearch, matches, mergeLive, parseFilters, toApiFilters } from "./historyFilters";

const me: Me = { id: 1, email: "me@x.com", name: "", role: "approver" };
const ch = (id: number, over: Partial<Change> = {}): Change => ({
  id, status: "applied", kind: "data", source: "web", requester: "me@x.com", prompt: "", createdAt: "2026-01-01T00:00:00Z",
  decidedBy: null, error: "", decisionNote: "", tables: [], schema: "master", diff: [], check: {}, ...over,
});

describe("parseFilters and filtersToSearch", () => {
  it("round-trips the defaults to an empty query", () => {
    expect(parseFilters("")).toEqual(DEFAULT_FILTERS);
    expect(filtersToSearch(DEFAULT_FILTERS)).toBe("");
  });
  it("round-trips set filters in a fixed order", () => {
    const f = parseFilters("?mine=1&status=applied&db=sales&kind=sql");
    expect(f).toEqual({ status: "applied", kind: "sql", db: "sales", mine: true });
    expect(filtersToSearch(f)).toBe("?status=applied&kind=sql&db=sales&mine=1");
  });
  it("treats junk as 'any'", () => {
    expect(parseFilters("?status=bogus&kind=x&mine=maybe&db=")).toEqual(DEFAULT_FILTERS);
    expect(parseFilters("?status=applied&status=failed")).toEqual({ ...DEFAULT_FILTERS, status: "applied" });
  });
  it("maps db to the api's schema", () => {
    expect(toApiFilters({ status: "failed", kind: "", db: "sales", mine: true })).toEqual({ status: "failed", schema: "sales", mine: true });
    expect(toApiFilters(DEFAULT_FILTERS)).toEqual({});
  });
});

describe("matches", () => {
  it("checks each filter", () => {
    expect(matches(ch(1), DEFAULT_FILTERS, me)).toBe(true);
    expect(matches(ch(1), { ...DEFAULT_FILTERS, status: "pending" }, me)).toBe(false);
    expect(matches(ch(1), { ...DEFAULT_FILTERS, kind: "sql" }, me)).toBe(false);
    expect(matches(ch(1, { kind: "sql" }), { ...DEFAULT_FILTERS, kind: "sql" }, me)).toBe(true);
    expect(matches(ch(1, { requester: "o@x.com" }), { ...DEFAULT_FILTERS, mine: true }, me)).toBe(false);
  });
  it("counts a change with no schema as master", () => {
    expect(matches(ch(1, { schema: undefined }), { ...DEFAULT_FILTERS, db: "master" }, me)).toBe(true);
    expect(matches(ch(1, { schema: undefined }), { ...DEFAULT_FILTERS, db: "sales" }, me)).toBe(false);
  });
});

describe("mergeLive", () => {
  it("replaces a stale row with its live copy", () => {
    const got = mergeLive([ch(5, { status: "pending" }), ch(4)], [ch(5, { status: "applied" })], DEFAULT_FILTERS, me);
    expect(got.map((c) => [c.id, c.status])).toEqual([[5, "applied"], [4, "applied"]]);
  });
  it("adds a newer matching live change on top and ignores a newer non-matching one", () => {
    const f = { ...DEFAULT_FILTERS, kind: "sql" as const };
    const got = mergeLive([ch(5, { kind: "sql" })], [ch(9, { kind: "sql" }), ch(10, { kind: "data" })], f, me);
    expect(got.map((c) => c.id)).toEqual([9, 5]);
  });
  it("ignores an older live change that is not loaded, and never duplicates", () => {
    const got = mergeLive([ch(5), ch(4)], [ch(2), ch(5), ch(5)], DEFAULT_FILTERS, me);
    expect(got.map((c) => c.id)).toEqual([5, 4]);
  });
  it("starts from nothing when no rows are loaded yet", () => {
    expect(mergeLive([], [ch(3)], DEFAULT_FILTERS, me)).toEqual([ch(3)]);
  });
});
