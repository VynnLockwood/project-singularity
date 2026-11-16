import { describe, expect, it } from "vitest";
import type { Role } from "./api";
import { atLeast, breadcrumb, NAV, navFor } from "./nav";

const views = (role: Role) => navFor(role).flatMap((g) => g.items.map((i) => i.view));

describe("navFor", () => {
  it("gives an editor every view, in four groups", () => {
    expect(views("editor")).toEqual(["chat", "database", "data", "query", "import", "duplicates", "scripts", "activity", "connect", "settings"]);
    expect(navFor("editor").map((g) => g.group)).toEqual(["Work", "Quality", "Review", "System"]);
  });
  it("hides Import from a viewer", () => {
    expect(views("viewer")).not.toContain("import");
    expect(views("approver")).toContain("import");
    expect(navFor("editor")[0].items.map((i) => i.view)).toEqual(["chat", "database", "data", "query", "import"]);
  });
  it("hides Scripts from a viewer and keeps Activity", () => {
    expect(views("viewer")).not.toContain("scripts");
    expect(views("viewer")).toContain("activity");
  });
  it("shows Users only to an admin, in System", () => {
    for (const role of ["viewer", "editor", "approver"] as const) expect(views(role)).not.toContain("users");
    expect(views("admin")).toEqual(["chat", "database", "data", "query", "import", "duplicates", "scripts", "activity", "connect", "settings", "users"]);
    expect(navFor("admin").find((g) => g.group === "System")?.items.map((i) => i.view)).toEqual(["connect", "settings", "users"]);
    expect(NAV.flatMap((g) => g.items).find((i) => i.view === "users")).toMatchObject({ label: "Users", minRole: "admin" });
  });
  it("keeps Profile out of the nav: it lives in the user menu", () => {
    expect(NAV.flatMap((g) => g.items).map((i) => i.view)).not.toContain("profile");
  });
});

describe("atLeast", () => {
  it("ranks viewer < editor < approver < admin", () => {
    expect(atLeast("admin", "approver")).toBe(true);
    expect(atLeast("approver", "approver")).toBe(true);
    expect(atLeast("editor", "approver")).toBe(false);
    expect(atLeast("approver", "admin")).toBe(false);
    expect(atLeast("viewer", "viewer")).toBe(true);
  });
});

describe("breadcrumb", () => {
  it("names the view, then the database and table", () => {
    expect(breadcrumb({ view: "data", schema: "master", table: "province" })).toEqual(["Data", "master", "province"]);
    expect(breadcrumb({ view: "settings", section: "rules" })).toEqual(["Settings", "Rules"]);
    expect(breadcrumb({ view: "chat" })).toEqual(["Chat"]);
    expect(breadcrumb({ view: "import", schema: "master", run: 3 })).toEqual(["Import", "master"]);
    expect(breadcrumb({ view: "notfound", path: "/nope" })).toEqual(["Page not found"]);
    expect(breadcrumb({ view: "users" })).toEqual(["Users"]);
    expect(breadcrumb({ view: "profile" })).toEqual(["Profile"]);
  });
});
