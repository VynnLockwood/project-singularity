import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { exportUrl, getGraph, getOptions, getRows, getSchemas, getTable, proposeChange, searchSchema } from "./api";

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
});
