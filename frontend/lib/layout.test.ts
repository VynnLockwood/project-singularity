import { describe, expect, it } from "vitest";
import type { GraphTable, SchemaGraph } from "./api";
import { CARD_W, layoutSchema } from "./layout";

const table = (name: string, cols: string[] = ["id"]): GraphTable => ({
  name, comment: null, rowEstimate: 0,
  columns: cols.map((c) => ({ name: c, type: "text", nullable: false, primaryKey: c === "id" })),
});
const rel = (a: string, ac: string, b: string, bc = "id") => ({ from: { table: a, column: ac }, to: { table: b, column: bc } });

describe("layoutSchema", () => {
  it("puts referencing tables to the right of what they reference", () => {
    const g: SchemaGraph = {
      tables: [table("orderLine", ["id", "orderId"]), table("orderHeader", ["id", "customerId"]), table("customer")],
      relations: [rel("orderLine", "orderId", "orderHeader"), rel("orderHeader", "customerId", "customer")],
    };
    const x = Object.fromEntries(layoutSchema(g).tables.map((p) => [p.table.name, p.x]));
    expect(x.customer).toBeLessThan(x.orderHeader);
    expect(x.orderHeader).toBeLessThan(x.orderLine);
  });

  it("never overlaps cards in the same column", () => {
    const g: SchemaGraph = { tables: [table("a"), table("b"), table("c")], relations: [] };
    const ps = layoutSchema(g).tables;
    for (let i = 1; i < ps.length; i++) expect(ps[i].y).toBeGreaterThanOrEqual(ps[i - 1].y + ps[i - 1].h);
  });

  it("draws an edge from the referencing column's row to the referenced card", () => {
    const g: SchemaGraph = {
      tables: [table("customer"), table("orderHeader", ["id", "customerId"])],
      relations: [rel("orderHeader", "customerId", "customer")],
    };
    const l = layoutSchema(g);
    const a = l.tables.find((p) => p.table.name === "orderHeader")!;
    const b = l.tables.find((p) => p.table.name === "customer")!;
    expect(l.edges).toHaveLength(1);
    expect(l.edges[0].x1).toBe(a.x);
    expect(l.edges[0].x2).toBe(b.x + CARD_W);
    expect(l.edges[0].y1).toBeGreaterThan(a.y);
  });

  it("survives cycles, self references and dangling relations", () => {
    const g: SchemaGraph = {
      tables: [table("a", ["id", "bId"]), table("b", ["id", "aId", "parentId"])],
      relations: [rel("a", "bId", "b"), rel("b", "aId", "a"), rel("b", "parentId", "b"), rel("b", "x", "ghost")],
    };
    const l = layoutSchema(g);
    expect(l.tables).toHaveLength(2);
    expect(l.edges).toHaveLength(3); // the dangling one is dropped
  });

  it("handles an empty schema", () => {
    const l = layoutSchema({ tables: [], relations: [] });
    expect(l.tables).toEqual([]);
    expect(l.width).toBeGreaterThan(0);
  });
});
