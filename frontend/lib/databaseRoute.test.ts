import { describe, expect, it } from "vitest";
import { parseTab, tabToSearch } from "./databaseRoute";

describe("databaseRoute", () => {
  it("reads the tab", () => {
    expect(parseTab("?tab=relations", true)).toBe("relations");
    expect(parseTab("?tab=erd", false)).toBe("erd");
  });
  it("defaults to fields with a table and overview without", () => {
    expect(parseTab("", true)).toBe("fields");
    expect(parseTab("", false)).toBe("overview");
  });
  it("junk and impossible values mean the default", () => {
    expect(parseTab("?tab=bogus", true)).toBe("fields");
    expect(parseTab("?tab=fields", false)).toBe("overview");
    expect(parseTab("?tab=relations", false)).toBe("overview");
  });
  it("repeated keys take the first", () => {
    expect(parseTab("?tab=erd&tab=relations", true)).toBe("erd");
  });
  it("writes only a non-default tab", () => {
    expect(tabToSearch("fields", true)).toBe("");
    expect(tabToSearch("overview", false)).toBe("");
    expect(tabToSearch("erd", true)).toBe("?tab=erd");
    expect(tabToSearch("overview", true)).toBe("?tab=overview");
  });
});
