import { describe, expect, it } from "vitest";
import { breadcrumb, navFor } from "./nav";

const views = (role: "viewer" | "editor" | "approver") => navFor(role).flatMap((g) => g.items.map((i) => i.view));

describe("navFor", () => {
  it("gives an editor every view, in four groups", () => {
    expect(views("editor")).toEqual(["chat", "database", "data", "query", "duplicates", "scripts", "activity", "connect", "settings"]);
    expect(navFor("editor").map((g) => g.group)).toEqual(["Work", "Quality", "Review", "System"]);
  });
  it("hides Scripts from a viewer and keeps Activity", () => {
    expect(views("viewer")).not.toContain("scripts");
    expect(views("viewer")).toContain("activity");
  });
});

describe("breadcrumb", () => {
  it("names the view, then the database and table", () => {
    expect(breadcrumb({ view: "data", schema: "master", table: "province" })).toEqual(["Data", "master", "province"]);
    expect(breadcrumb({ view: "settings", section: "rules" })).toEqual(["Settings", "Rules"]);
    expect(breadcrumb({ view: "chat" })).toEqual(["Chat"]);
    expect(breadcrumb({ view: "notfound", path: "/nope" })).toEqual(["Page not found"]);
  });
});
