import { describe, expect, it } from "vitest";
import type { Change, Me } from "./api";
import { change } from "./fixtures";
import { canDecide, changesRoles, diffKind, upsert, warnings } from "./changes";

const me = (role: Me["role"], email = "ed@example.com"): Me => ({ id: 1, email, name: "", role, active: true });

describe("canDecide mirrors the server rule", () => {
  it("approvers decide anything pending", () => {
    expect(canDecide(me("approver", "x@x.com"), change())).toBe(true);
    expect(canDecide(me("approver", "x@x.com"), change({ kind: "data" }))).toBe(true);
  });
  it("only an admin decides a role change", () => {
    const roles = change({ kind: "config", operations: [{ op: "set_setting" }, { op: "set_user_role" }] });
    expect(canDecide(me("approver", "x@x.com"), roles)).toBe(false);
    expect(canDecide(me("admin", "x@x.com"), roles)).toBe(true);
    expect(canDecide(me("approver", "x@x.com"), change({ kind: "config", operations: [{ op: "set_setting" }] }))).toBe(true);
    expect(canDecide(me("editor"), roles)).toBe(false);
    expect(changesRoles(roles)).toBe(true);
    expect(changesRoles(change({ kind: "data", operations: [{ op: "set_user_role" }] }))).toBe(false);
  });
  it("admins decide anything an approver can", () => {
    expect(canDecide(me("admin", "x@x.com"), change())).toBe(true);
    expect(canDecide(me("admin", "x@x.com"), change({ kind: "config" }))).toBe(true);
  });
  it("editors decide only their own data changes", () => {
    expect(canDecide(me("editor"), change({ kind: "data" }))).toBe(true);
    expect(canDecide(me("editor", "other@example.com"), change({ kind: "data" }))).toBe(false);
    expect(canDecide(me("editor"), change({ kind: "schema" }))).toBe(false);
  });
  it("viewers never decide, and nobody decides a finished change", () => {
    expect(canDecide(me("viewer"), change())).toBe(false);
    expect(canDecide(me("approver"), change({ status: "applied" }))).toBe(false);
  });
});

describe("helpers", () => {
  it("classifies diff lines", () => {
    expect(["+ a", "- b", "~ c"].map(diffKind)).toEqual(["add", "remove", "change"]);
  });
  it("upsert replaces by id and keeps order", () => {
    const list = upsert(upsert([], change({ id: 2 })), change({ id: 1 }));
    expect(list.map((c) => c.id)).toEqual([1, 2]);
    expect(upsert(list, change({ id: 1, status: "applied" })).find((c) => c.id === 1)?.status).toBe("applied");
    expect(upsert(list, change({ id: 1 }))).toHaveLength(2);
  });
  it("keeps only warnings", () => {
    const c = change({ check: { violations: [{ severity: "warn", message: "w" }, { severity: "block", message: "b" }] } });
    expect(warnings(c).map((v) => v.message)).toEqual(["w"]);
  });
});
