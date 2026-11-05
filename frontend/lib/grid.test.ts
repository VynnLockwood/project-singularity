import { describe, expect, it } from "vitest";
import type { GridColumn } from "./api";
import { buildOperations, coerce, draftSize, emptyDraft, validate } from "./grid";

const col = (name: string, type: string, o: Partial<GridColumn> = {}): GridColumn =>
  ({ name, type, nullable: false, readOnly: false, references: null, ...o });
const columns = [
  col("id", "bigint", { readOnly: true }), col("name", "text"), col("qty", "integer"),
  col("credit", "numeric(12,2)", { nullable: true }), col("active", "boolean"), col("customerId", "bigint", { references: { table: "customer", column: "id" } }),
];

describe("coerce", () => {
  it.each([
    ["integer", "42", 42], ["bigint", "-7", -7], ["smallint", "0", 0],
    ["numeric(12,2)", "10.50", "10.50"], ["double precision", "1.5", 1.5],
    ["boolean", "true", true], ["boolean", "false", false],
    ["text", "สวัสดี", "สวัสดี"], ["character varying(20)", "x", "x"],
    ["date", "2026-11-05", "2026-11-05"], ["jsonb", '{"a":1}', { a: 1 }],
    ["uuid", "123e4567-e89b-12d3-a456-426614174000", "123e4567-e89b-12d3-a456-426614174000"],
  ])("%s %s", (type, raw, value) => {
    expect(coerce({ type, nullable: false }, raw)).toEqual({ ok: true, value });
  });

  it.each([
    ["integer", "4.5"], ["integer", "abc"], ["numeric(12,2)", "1,5"], ["boolean", "yes"],
    ["date", "05/11/2026"], ["date", "2026-13-45"], ["uuid", "nope"], ["jsonb", "{bad"], ["timestamp with time zone", "not a time"],
  ])("rejects %s %s", (type, raw) => {
    expect(coerce({ type, nullable: false }, raw).ok).toBe(false);
  });

  it("empty means null only where allowed", () => {
    expect(coerce({ type: "numeric(12,2)", nullable: true }, "")).toEqual({ ok: true, value: null });
    expect(coerce({ type: "integer", nullable: false }, "").ok).toBe(false);
    expect(coerce({ type: "text", nullable: false }, "")).toEqual({ ok: true, value: "" });
  });

  it("keeps integers beyond the safe range exact as text", () => {
    expect(coerce({ type: "bigint", nullable: false }, "9007199254740993")).toEqual({ ok: true, value: "9007199254740993" });
  });
});

describe("buildOperations", () => {
  it("groups identical edits into one update over a list of ids", () => {
    const d = emptyDraft();
    d.edits = { "1": { qty: "5" }, "2": { qty: "5" }, "3": { qty: "9" }, "4": { name: "x", qty: "5" } };
    expect(buildOperations("item", columns, d)).toEqual([
      { op: "update_rows", table: "item", where: { id: [1, 2] }, set: { qty: 5 } },
      { op: "update_rows", table: "item", where: { id: 3 }, set: { qty: 9 } },
      { op: "update_rows", table: "item", where: { id: 4 }, set: { name: "x", qty: 5 } },
    ]);
  });

  it("sends only what was typed for new rows and skips read-only columns", () => {
    const d = emptyDraft();
    d.added = [{ name: "A", qty: "1", id: "99" }, { name: "B", credit: "" }, {}];
    expect(buildOperations("item", columns, d)).toEqual([
      { op: "insert_rows", table: "item", rows: [{ name: "A", qty: 1 }, { name: "B" }] },
    ]);
  });

  it("removing a row deactivates it and drops its pending edits", () => {
    const d = emptyDraft();
    d.edits = { "1": { qty: "5" }, "2": { qty: "6" } };
    d.removed = [1, 3];
    expect(buildOperations("item", columns, d)).toEqual([
      { op: "update_rows", table: "item", where: { id: 2 }, set: { qty: 6 } },
      { op: "deactivate_rows", table: "item", where: { id: [1, 3] } },
    ]);
  });

  it("an empty draft produces nothing", () => {
    expect(buildOperations("item", columns, emptyDraft())).toEqual([]);
  });

  it("reference columns send the chosen id", () => {
    const d = emptyDraft();
    d.edits = { "1": { customerId: "12" } };
    expect(buildOperations("orderHeader", columns, d)[0]).toMatchObject({ set: { customerId: 12 } });
  });
});

describe("validate and draftSize", () => {
  it("reports bad cells by position and ignores cells of removed rows", () => {
    const d = emptyDraft();
    d.edits = { "1": { qty: "abc" }, "2": { qty: "oops" } };
    d.added = [{ qty: "1.5" }];
    d.removed = [2];
    expect(validate(columns, d)).toEqual({ "1:qty": "whole number", "new0:qty": "whole number" });
  });

  it("counts every pending change", () => {
    const d = emptyDraft();
    d.edits = { "1": { qty: "1", name: "x" } };
    d.added = [{}];
    d.removed = [5];
    expect(draftSize(d)).toBe(4);
  });
});
