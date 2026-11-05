import { describe, expect, it } from "vitest";
import cases from "../../engine/testdata/stats_cases.json";
import { columnStats, computeStats } from "./stats";

describe("columnStats matches the Rust engine on the shared cases", () => {
  for (const c of cases as { name: string; input: unknown[]; expected: unknown }[]) {
    it(c.name, () => expect(columnStats(c.input)).toEqual(c.expected));
  }
});

describe("computeStats", () => {
  it("falls back to TypeScript when WebAssembly is not available", async () => {
    const r = await computeStats([1, 2, null]);
    expect(r.engine).toBe("js");
    expect(r.stats.nullPct).toBe(33.333);
  });
});
