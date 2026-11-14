import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { commentsAbove, isCommentLine, sqlContextAbove, statementAt, suggestQuery } from "./iquery";

describe("commentsAbove", () => {
  it("collects -- lines above the cursor, oldest first, with the cursor line", () => {
    expect(commentsAbove("-- a\nselect 1;\n-- b\n", 18)).toEqual(["a", "b"]);
  });
  it("drops empty comments and keeps only the last 10", () => {
    expect(commentsAbove("--\n-- x\n", 8)).toEqual(["x"]);
    const text = Array.from({ length: 12 }, (_, i) => `-- c${i}`).join("\n") + "\n";
    const got = commentsAbove(text, text.length);
    expect(got).toHaveLength(10);
    expect(got[0]).toBe("c2");
    expect(got[9]).toBe("c11");
  });
  it("ignores comments below the cursor", () => {
    expect(commentsAbove("-- a\n\n-- later\n", 5)).toEqual(["a"]);
  });
});

describe("sqlContextAbove", () => {
  it("is the non-comment text above the cursor", () => {
    expect(sqlContextAbove("select 1;\n-- b\n", 14)).toBe("select 1;");
  });
  it("keeps the last 4000 characters", () => {
    const text = "A".repeat(5000) + "TAIL\n-- c";
    const out = sqlContextAbove(text, text.length);
    expect(out).toHaveLength(4000);
    expect(out.endsWith("TAIL")).toBe(true);
  });
});

describe("isCommentLine", () => {
  it("sees a leading --", () => {
    expect(isCommentLine("  -- hi")).toBe(true);
    expect(isCommentLine("select 1 -- hi")).toBe(false);
  });
});

describe("statementAt", () => {
  it("ignores ; inside strings and comments", () => {
    const t = "select 'a;b' as x; -- y;\nselect 2";
    expect(statementAt(t, 3)?.sql).toBe("select 'a;b' as x");
  });
  it("returns the second statement for a cursor in it", () => {
    const t = "select 1;\n-- why\nselect 2;";
    const s = statementAt(t, t.indexOf("select 2") + 3);
    expect(s?.sql).toBe("select 2");
    expect(t.slice(s!.from, s!.to)).toContain("select 2");
  });
  it("skips $$ bodies and drops comment lines", () => {
    const t = "-- note\nselect $$a;b$$;";
    expect(statementAt(t, 12)?.sql).toBe("select $$a;b$$");
  });
  it("is null for a blank statement", () => {
    expect(statementAt("select 1;\n  \n", 12)).toBeNull();
  });
});

describe("suggestQuery", () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  beforeEach(() => {
    document.cookie = "csrftoken=t";
    calls.length = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return { ok: true, status: 200, json: async () => ({ sql: "select 1", source: "model", tables: [], values: [], requestId: "r" }) };
    }));
  });
  afterEach(() => vi.unstubAllGlobals());

  it("posts the body with the api key and the abort signal", async () => {
    window.localStorage.setItem("singularity.chatApiKey", "sk-1");
    const ac = new AbortController();
    await suggestQuery({ schema: "master", comments: ["a"], requestId: "r" }, ac.signal);
    expect(calls[0].url).toBe("/api/intelligent-query/suggest");
    expect(JSON.parse(calls[0].init!.body as string)).toEqual({ schema: "master", comments: ["a"], requestId: "r", apiKey: "sk-1" });
    expect(calls[0].init!.signal).toBe(ac.signal);
    window.localStorage.removeItem("singularity.chatApiKey");
  });
});
