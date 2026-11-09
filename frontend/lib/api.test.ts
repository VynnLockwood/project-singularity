import { describe, expect, it } from "vitest";
import { exportUrl } from "./api";

describe("exportUrl", () => {
  it("builds the export link", () => {
    expect(exportUrl("master", true)).toBe("/api/schema/export?schema=master&data=1");
    expect(exportUrl("master", false)).toBe("/api/schema/export?schema=master&data=0");
  });
  it("encodes the schema name", () => {
    expect(exportUrl("a b", false)).toBe("/api/schema/export?schema=a%20b&data=0");
  });
});
