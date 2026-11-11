import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { csvUrl, exportUrl, getGraph, getOptions, getRows, getSchemas, getTable, listChanges, proposeChange, revertChange, searchSchema } from "./api";

describe("exportUrl", () => {
  it("builds the export link", () => {
    expect(exportUrl("master", true)).toBe("/api/schema/export?schema=master&data=1");
    expect(exportUrl("master", false)).toBe("/api/schema/export?schema=master&data=0");
  });
  it("encodes the schema name", () => {
    expect(exportUrl("a b", false)).toBe("/api/schema/export?schema=a%20b&data=0");
  });
});

describe("schema-aware calls", () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  const respond = (body: unknown) => {
    calls.length = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return { ok: true, status: 200, json: async () => body };
    }));
  };
  beforeEach(() => {
    document.cookie = "csrftoken=t";
  });
  afterEach(() => vi.unstubAllGlobals());

  it("getSchemas unwraps the list", async () => {
    respond({ schemas: [{ name: "master", tables: 2, comment: null }] });
    expect(await getSchemas()).toEqual([{ name: "master", tables: 2, comment: null }]);
    expect(calls[0].url).toBe("/api/schema/schemas");
  });
  it("getGraph: master unchanged, other schema adds the param", async () => {
    respond({ tables: [], relations: [] });
    await getGraph();
    expect(calls[0].url).toBe("/api/schema/graph");
    await getGraph("crm");
    expect(calls[1].url).toBe("/api/schema/graph?schema=crm");
  });
  it("getTable", async () => {
    respond({ name: "t", hiddenColumns: [] });
    await getTable("t");
    await getTable("t", "crm");
    expect(calls.map((c) => c.url)).toEqual(["/api/schema/tables/t", "/api/schema/tables/t?schema=crm"]);
  });
  it("getRows builds one query string", async () => {
    respond({});
    await getRows("t", { offset: 0, limit: 10 });
    expect(calls[0].url).toBe("/api/schema/tables/t/rows?offset=0&limit=10");
    await getRows("t", { offset: 0, limit: 10, filters: { a: "x" } }, "crm");
    const u = calls[1].url;
    expect(u.split("?").length).toBe(2);
    const p = new URLSearchParams(u.split("?")[1]);
    expect(p.get("schema")).toBe("crm");
    expect(p.get("filters")).toBe('{"a":"x"}');
  });
  it("getOptions", async () => {
    respond({});
    await getOptions("t");
    await getOptions("t", "crm");
    expect(calls.map((c) => c.url)).toEqual([
      "/api/schema/tables/t/options?limit=200",
      "/api/schema/tables/t/options?limit=200&schema=crm",
    ]);
  });
  it("proposeChange sends schema only when not master", async () => {
    respond({ id: 1 });
    await proposeChange([], "p", "web");
    expect(JSON.parse(calls.at(-1)!.init!.body as string)).toEqual({ operations: [], source: "web", prompt: "p" });
    await proposeChange([], "p", "web", "crm");
    expect(JSON.parse(calls.at(-1)!.init!.body as string).schema).toBe("crm");
  });
  it("searchSchema", async () => {
    respond({ results: [] });
    await searchSchema("x");
    await searchSchema("x", "crm");
    expect(calls.map((c) => c.url)).toEqual(["/api/search?q=x", "/api/search?q=x&schema=crm"]);
  });

  it("listChanges sends only the set filters and the limit, and reports more", async () => {
    respond({ changes: [{ id: 1 }], more: true });
    expect(await listChanges({ status: "applied", mine: true }, 100)).toEqual({ changes: [{ id: 1 }], more: true });
    expect(calls[0].url).toBe("/api/changes?status=applied&mine=1&limit=100");
    respond({ changes: [] });
    expect(await listChanges({ schema: "sales", kind: "sql" }, 50)).toEqual({ changes: [], more: false });
    expect(calls[0].url).toBe("/api/changes?kind=sql&schema=sales&limit=50");
  });
  it("revertChange posts to the revert action", async () => {
    respond({ id: 9, status: "applied" });
    await revertChange(7);
    expect(calls[0].url).toBe("/api/changes/7/revert");
    expect(calls[0].init?.method).toBe("POST");
  });
});

describe("csvUrl", () => {
  it("carries the view", () => {
    const u = csvUrl("order header", "master", { sort: "name", dir: "desc", filters: { name: "a" }, inactive: true });
    expect(u).toContain("order%20header/export.csv");
    expect(u).toContain("sort=name");
    expect(u).toContain("dir=desc");
    expect(u).toContain("inactive=1");
    expect(u).toContain("filters=%7B%22name%22%3A%22a%22%7D");
  });
  it("has only the schema for an empty view", () => {
    expect(csvUrl("t", "master", { sort: null, dir: "asc", filters: {}, inactive: false })).toBe("/api/schema/tables/t/export.csv?schema=master");
  });
});
