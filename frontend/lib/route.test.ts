import { describe, expect, it } from "vitest";
import { buildPath, parseRoute, type Route } from "./route";

describe("parseRoute", () => {
  it("opens chat at the root", () => expect(parseRoute("/")).toEqual({ view: "chat" }));
  it("reads a conversation id, ignoring one that is not a number", () => {
    expect(parseRoute("/chat/12")).toEqual({ view: "chat", conversation: 12 });
    expect(parseRoute("/chat/abc")).toEqual({ view: "chat" });
    expect(parseRoute("/chat")).toEqual({ view: "chat" });
  });
  it("reads schema and table for database and data", () => {
    expect(parseRoute("/database/master/province")).toEqual({ view: "database", schema: "master", table: "province" });
    expect(parseRoute("/database/master")).toEqual({ view: "database", schema: "master" });
    expect(parseRoute("/data/sales/customer")).toEqual({ view: "data", schema: "sales", table: "customer" });
    expect(parseRoute("/data")).toEqual({ view: "data" });
  });
  it("reads a known settings section only", () => {
    expect(parseRoute("/settings/rules")).toEqual({ view: "settings", section: "rules" });
    expect(parseRoute("/settings/zzz")).toEqual({ view: "settings" });
  });
  it("reads the plain views", () => {
    for (const v of ["duplicates", "scripts", "activity", "connect"] as const) expect(parseRoute(`/${v}`)).toEqual({ view: v });
  });
  it("keeps the old queue address working", () => expect(parseRoute("/queue")).toEqual({ view: "activity" }));
  it("decodes segments and survives odd paths", () => {
    expect(parseRoute("/data/master/my%20table")).toEqual({ view: "data", schema: "master", table: "my table" });
    expect(parseRoute("/data/master/")).toEqual({ view: "data", schema: "master" });
    expect(parseRoute("//data//master")).toEqual({ view: "data", schema: "master" });
    expect(parseRoute("")).toEqual({ view: "chat" });
  });
  it("treats a malformed escape as not found instead of throwing", () => {
    expect(parseRoute("/data/%E0%A4%A")).toEqual({ view: "notfound", path: "/data/%E0%A4%A" });
  });
  it("reports an unknown place", () => expect(parseRoute("/nope")).toEqual({ view: "notfound", path: "/nope" }));
});

describe("buildPath", () => {
  const samples: Route[] = [
    { view: "chat" }, { view: "chat", conversation: 7 },
    { view: "database" }, { view: "database", schema: "master" }, { view: "database", schema: "master", table: "province" },
    { view: "data", schema: "sales", table: "my table" },
    { view: "settings" }, { view: "settings", section: "team" },
    { view: "duplicates" }, { view: "scripts" }, { view: "activity" }, { view: "connect" },
  ];
  it.each(samples)("round trips %o", (r) => expect(parseRoute(buildPath(r))).toEqual(r));
  it("puts chat at the root and encodes segments", () => {
    expect(buildPath({ view: "chat" })).toBe("/");
    expect(buildPath({ view: "data", schema: "sales", table: "my table" })).toBe("/data/sales/my%20table");
  });
  it("drops a table that has no schema", () => expect(buildPath({ view: "data", table: "x" })).toBe("/data"));
  it("gives back the path of an unknown place", () => expect(buildPath({ view: "notfound", path: "/nope" })).toBe("/nope"));
});

describe("isAssetPath", () => {
  it("spots a request for a file, so the app page is not served in its place", async () => {
    const { isAssetPath } = await import("./route");
    expect(isAssetPath(["engine", "singularity_engine.js"])).toBe(true);
    expect(isAssetPath(["favicon.ico"])).toBe(true);
    expect(isAssetPath(["engine", "singularity_engine_bg.wasm"])).toBe(true);
  });
  it("leaves app places alone, including names with a dot in the middle of the path", async () => {
    const { isAssetPath } = await import("./route");
    expect(isAssetPath(undefined)).toBe(false);
    expect(isAssetPath(["data", "master", "province"])).toBe(false);
    expect(isAssetPath(["nope"])).toBe(false);
  });
});
