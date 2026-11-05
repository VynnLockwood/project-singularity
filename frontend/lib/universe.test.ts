import { describe, expect, it } from "vitest";
import cases from "../../engine/testdata/layout_cases.json";
import type { SchemaGraph } from "./api";
import { bodySize, layout3d, layoutUniverse } from "./universe";

describe("layout3d (TypeScript twin of the Rust engine)", () => {
  for (const c of cases as { name: string; input: { nodes: number; edges: [number, number][]; iterations?: number; radius?: number }; expected: { positions: number[][] } }[]) {
    it(`matches the Rust golden case: ${c.name}`, () => {
      const got = layout3d(c.input.nodes, c.input.edges, c.input.iterations ?? 200, c.input.radius ?? 100);
      expect(got).toHaveLength(c.expected.positions.length);
      got.forEach((p, i) => p.forEach((v, k) => expect(Math.abs(v - c.expected.positions[i][k])).toBeLessThan(1e-6)));
    });
  }

  it("handles tiny graphs", () => {
    expect(layout3d(0, [])).toEqual([]);
    expect(layout3d(1, [])).toEqual([[0, 0, 0]]);
  });
});

const col = (name: string) => ({ name, type: "text", nullable: true, primaryKey: false, comment: null, default: null } as never);
const graph: SchemaGraph = {
  tables: [
    { name: "customer", comment: null, rowEstimate: 1000, columns: [col("id"), col("name")] },
    { name: "orderHeader", comment: null, rowEstimate: 0, columns: [col("id"), col("customerId")] },
  ],
  relations: [
    { from: { table: "orderHeader", column: "customerId" }, to: { table: "customer", column: "id" } },
    { from: { table: "ghost", column: "x" }, to: { table: "customer", column: "id" } },
  ],
};

describe("layoutUniverse", () => {
  it("makes a body per table and a link per known relation", async () => {
    const u = await layoutUniverse(graph);
    expect(u.bodies.map((b) => b.name)).toEqual(["customer", "orderHeader"]);
    expect(u.links).toEqual([{ from: "orderHeader", to: "customer", fromColumn: "customerId" }]);
    expect(u.engine).toBe("js");
    u.bodies.forEach((b) => expect(b.pos.every(Number.isFinite)).toBe(true));
  });

  it("sizes bodies by row count, slowly", () => {
    expect(bodySize(0)).toBeLessThan(bodySize(1000));
    expect(bodySize(1_000_000)).toBeLessThan(bodySize(1000) * 3);
  });
});
