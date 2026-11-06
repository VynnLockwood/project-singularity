import { describe, expect, it } from "vitest";
import cases from "../../engine/testdata/cosmos_cases.json";
import { cosmos, loadCosmos } from "./cosmos";

describe("cosmos (TypeScript twin of the Rust engine)", () => {
  for (const c of cases as { name: string; input: { seed: number; stars: number; particles: number }; expected: ReturnType<typeof cosmos> }[]) {
    it(`matches the Rust golden case: ${c.name}`, () => {
      const got = cosmos(c.input.seed, c.input.stars, c.input.particles);
      expect(got.stars).toHaveLength(c.expected.stars.length);
      expect(got.disk).toHaveLength(c.expected.disk.length);
      got.stars.forEach((s, i) => (Object.keys(s) as (keyof typeof s)[]).forEach((k) => expect(Math.abs(s[k] - c.expected.stars[i][k])).toBeLessThan(1e-9)));
      got.disk.forEach((p, i) => (Object.keys(p) as (keyof typeof p)[]).forEach((k) => expect(Math.abs(p[k] - c.expected.disk[i][k])).toBeLessThan(1e-9)));
    });
  }

  it("is deterministic and falls back to JavaScript without the WASM build", async () => {
    expect(cosmos(5, 10, 10)).toEqual(cosmos(5, 10, 10));
    expect((await loadCosmos(5, 10, 10)).engine).toBe("js");
  });
});
