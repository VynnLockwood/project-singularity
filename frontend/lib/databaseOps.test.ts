import { describe, expect, it } from "vitest";
import * as o from "./databaseOps";

describe("validateName", () => {
  it("accepts good names", () => {
    expect(o.validateName("customer", [])).toBeNull();
    expect(o.validateName("a1_b", [])).toBeNull();
  });
  it("refuses bad names", () => {
    for (const n of ["", "Customer", "has space", "1abc", "a".repeat(64)]) expect(o.validateName(n, [])).toBeTruthy();
    expect(o.validateName("a".repeat(63), [])).toBeNull();
    expect(o.validateName("customer", ["customer"])).toMatch(/already exists/);
  });
});

describe("validateSchemaName", () => {
  it("refuses reserved names", () => {
    for (const n of ["public", "singularity", "information_schema", "pg_x"]) expect(o.validateSchemaName(n, [])).toMatch(/reserved/);
    expect(o.validateSchemaName("sales", [])).toBeNull();
    expect(o.validateSchemaName("sales", ["sales"])).toMatch(/already exists/);
    expect(o.validateSchemaName("Sales", [])).toBeTruthy();
  });
});

describe("validateType", () => {
  it("accepts offered types and forms", () => {
    for (const t of o.COLUMN_TYPES.filter((t) => !t.includes("("))) expect(o.validateType(t)).toBeNull();
    for (const t of ["numeric(10,2)", "numeric(10, 2)", "varchar(40)", "Double  Precision"]) expect(o.validateType(t)).toBeNull();
  });
  it("refuses others", () => {
    for (const t of ["varchar", "varchar(0)", "serial", "text; drop", "numeric(2,5)", "varchar(10000)"]) expect(o.validateType(t)).toBeTruthy();
  });
});

describe("parseDefault", () => {
  it.each([
    ["", undefined], ["now()", "now()"], ["gen_random_uuid()", "gen_random_uuid()"], ["current_date", "current_date"],
    ["5", 5], ["-1.5", -1.5], ["true", true], ["false", false], ["null", null], ["abc", "abc"],
  ])("%j", (i, e) => expect(o.parseDefault(i)).toEqual(e));
});

describe("builders", () => {
  it("schema ops", () => {
    expect(o.createSchemaOp("sales")).toStrictEqual({ op: "create_schema", name: "sales" });
    expect(o.createSchemaOp("sales", "hi")).toStrictEqual({ op: "create_schema", name: "sales", comment: "hi" });
    expect(o.renameSchemaOp("a", "b")).toEqual({ op: "rename_schema", schema: "a", new_name: "b" });
    expect(o.dropSchemaOp("a", "old")).toEqual({ op: "drop_schema", schema: "a", reason: "old" });
  });
  it("create_table omits empty optionals", () => {
    const op = o.createTableOp("t", [
      { name: "a", type: "text" },
      { name: "b", type: "integer", nullable: false, unique: true, default: "5", comment: "c", references: { table: "x", column: "id" } },
      { name: "c", type: "text", default: "", comment: "", references: { table: "y" } },
    ]);
    expect(op).toStrictEqual({
      op: "create_table", name: "t",
      columns: [
        { name: "a", type: "text" },
        { name: "b", type: "integer", nullable: false, unique: true, default: 5, comment: "c", references: { table: "x", column: "id" } },
        { name: "c", type: "text", references: { table: "y" } },
      ],
    });
    expect(o.createTableOp("t", [{ name: "a", type: "text" }], "cm")).toHaveProperty("comment", "cm");
  });
  it("table and column ops", () => {
    expect(o.addColumnOp("t", { name: "p", type: "text" })).toStrictEqual({ op: "add_column", table: "t", column: { name: "p", type: "text" } });
    expect(o.renameTableOp("t", "u")).toEqual({ op: "rename_table", table: "t", new_name: "u" });
    expect(o.renameColumnOp("t", "a", "b")).toEqual({ op: "rename_column", table: "t", column: "a", new_name: "b" });
    expect(o.alterColumnTypeOp("t", "a", "bigint")).toEqual({ op: "alter_column_type", table: "t", column: "a", type: "bigint" });
    expect(o.setColumnCommentOp("t", "a", "x")).toEqual({ op: "set_column_comment", table: "t", column: "a", comment: "x" });
    expect(o.dropColumnOp("t", "a", "why")).toEqual({ op: "drop_column", table: "t", column: "a", reason: "why" });
    expect(o.dropTableOp("t", "why")).toEqual({ op: "drop_table", table: "t", reason: "why" });
  });
  it("index and constraint ops", () => {
    expect(o.addIndexOp("t", ["a"])).toStrictEqual({ op: "add_index", table: "t", columns: ["a"] });
    expect(o.addIndexOp("t", ["a"], true)).toStrictEqual({ op: "add_index", table: "t", columns: ["a"], unique: true });
    expect(o.addUniqueOp("t", ["a", "b"])).toEqual({ op: "add_unique", table: "t", columns: ["a", "b"] });
    expect(o.addForeignKeyOp("t", "a", "r")).toStrictEqual({ op: "add_foreign_key", table: "t", column: "a", ref_table: "r" });
    expect(o.addForeignKeyOp("t", "a", "r", "id", "set null")).toStrictEqual({
      op: "add_foreign_key", table: "t", column: "a", ref_table: "r", ref_column: "id", on_delete: "set null",
    });
    expect(o.dropIndexOp("i", "why")).toEqual({ op: "drop_index", name: "i", reason: "why" });
    expect(o.dropConstraintOp("t", "c", "why")).toEqual({ op: "drop_constraint", table: "t", name: "c", reason: "why" });
  });
});

describe("isDestructive", () => {
  it("is true only for the five drops", () => {
    const yes = [o.dropColumnOp("t", "a", "r"), o.dropTableOp("t", "r"), o.dropSchemaOp("s", "r"), o.dropIndexOp("i", "r"), o.dropConstraintOp("t", "c", "r")];
    yes.forEach((op) => expect(o.isDestructive(op)).toBe(true));
    const no = [o.createSchemaOp("s"), o.createTableOp("t", [{ name: "a", type: "text" }]), o.addColumnOp("t", { name: "a", type: "text" }), o.renameTableOp("t", "u"), o.addIndexOp("t", ["a"])];
    no.forEach((op) => expect(o.isDestructive(op)).toBe(false));
  });
});

describe("describeOp", () => {
  it("describes", () => {
    expect(o.describeOp(o.createTableOp("t", [{ name: "a", type: "text" }, { name: "b", type: "text" }]))).toBe("Create table t (2 columns)");
    expect(o.describeOp(o.addColumnOp("customer", { name: "phone", type: "text" }))).toBe("Add column customer.phone (text)");
    expect(o.describeOp(o.dropColumnOp("customer", "phone", "r"))).toBe("Drop column customer.phone");
    expect(o.describeOp(o.createSchemaOp("sales"))).toBe("Create database sales");
    expect(o.describeOp(o.dropSchemaOp("sales", "r"))).toBe("Drop database sales");
  });
});
