import { describe, expect, it } from "vitest";
import { EMPTY_VIEW, parseView, viewToSearch } from "./dataRoute";

describe("dataRoute", () => {
  it("round trips sort, dir, inactive and filters", () => {
    const v = { sort: "name", dir: "desc" as const, inactive: true, filters: { name: "ab" } };
    expect(viewToSearch(v)).toBe("?sort=name&dir=desc&inactive=1&f.name=ab");
    expect(parseView(viewToSearch(v))).toEqual(v);
  });
  it("empty means empty", () => {
    expect(parseView("")).toEqual(EMPTY_VIEW);
    expect(viewToSearch(EMPTY_VIEW)).toBe("");
  });
  it("junk means none", () => {
    expect(parseView("?sort=name&dir=up").dir).toBe("asc");
    expect(parseView("?inactive=2").inactive).toBe(false);
    expect(parseView("?f.x=&f.y=1").filters).toEqual({ y: "1" });
    expect(parseView("?sort=a&sort=b").sort).toBe("a");
    expect(parseView("?dir=desc").sort).toBeNull();
  });
  it("omits dir when there is no sort, and asc", () => {
    expect(viewToSearch({ ...EMPTY_VIEW, dir: "desc" })).toBe("");
    expect(viewToSearch({ ...EMPTY_VIEW, sort: "a" })).toBe("?sort=a");
  });
  it("column names with dots and spaces and odd filter text survive", () => {
    const v = { ...EMPTY_VIEW, filters: { "a.b c": "x&y=z#1 สมชาย" } };
    expect(parseView(viewToSearch(v))).toEqual(v);
  });
});
