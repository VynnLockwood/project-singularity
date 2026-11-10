import { describe, expect, it } from "vitest";
import { match, rank, type Command } from "./palette";

const cmd = (label: string, group: Command["group"] = "Go to"): Command => ({ id: label, label, group, icon: "dot", run: () => {} });

describe("match", () => {
  it("matches letters in order, anywhere in the label", () => {
    expect(match("prv", "province")).not.toBeNull();
    expect(match("xyz", "province")).toBeNull();
    expect(match("ecnivorp", "province")).toBeNull();
  });
  it("ignores case", () => expect(match("PROV", "master.Province")).not.toBeNull());
  it("matches everything for an empty query", () => expect(match("", "anything")).toBe(0));
  it("scores an early, tight match better than a late or loose one", () => {
    expect(match("pro", "province")!).toBeLessThan(match("pro", "approve")!);
    expect(match("set", "Settings")!).toBeLessThan(match("set", "sales.target")!);
  });
});

describe("rank", () => {
  const all = [cmd("New chat", "Actions"), cmd("master.province", "Tables"), cmd("Go to Settings"), cmd("Go to Chat")];
  it("keeps the group order when nothing is typed", () => {
    expect(rank("", all).map((c) => c.group)).toEqual(["Go to", "Go to", "Tables", "Actions"]);
  });
  it("puts the best match first and drops what does not match", () => {
    expect(rank("prov", all).map((c) => c.label)).toEqual(["master.province"]);
    expect(rank("chat", all).map((c) => c.label)).toEqual(["New chat", "Go to Chat"]);
  });
  it("honours the limit", () => expect(rank("", all, 2)).toHaveLength(2));
});
