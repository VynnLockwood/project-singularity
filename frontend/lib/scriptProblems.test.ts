import { describe, expect, it } from "vitest";

import { explain } from "./scriptProblems";

const e = (kind: string, message: string, line = 7, statement = "STMT") => ({ kind, line, statement, message });

describe("scriptProblems.explain", () => {
  it.each([
    ["not valid SQL", e("parse", 'not valid SQL: syntax error at or near "FORM"'), /not valid SQL/i, /line 7/i],
    ["not allowed", e("parse", "DROP TABLE is not allowed in a script (allowed: CREATE TABLE, ...)"), /delete or change/i, /remove/i],
    ["type", e("parse", "column code: type geometry is not supported"), /column type/i, /supported type/i],
    ["default", e("parse", "default random() is not supported (allowed: constants, now())"), /default value/i, /DEFAULT/],
    ["nothing", e("parse", "Nothing to import: the file has no tables or rows.", 0, ""), /nothing to import/i, /CREATE TABLE/],
    ["twice", e("parse", "table 'a' is created twice"), /twice/i, /once/i],
    ["long name", e("parse", "identifier 'xxxx' is longer than 63 characters"), /too long/i, /63/],
    ["row width", e("rows", "row has 3 fields, expected 4"), /wrong number of columns/i, /tab/i],
    ["utf8", e("storage", "Script is not valid UTF-8 text.", 0, ""), /UTF-8/, /Save/i],
    ["mapping", e("mapping", "tables.a.target must be snake_case", 0, "tables.a"), /destination/i, /Customize/],
    ["too many ops", e("rows", "script has 1200 operations, above the limit maxOpsPerScript (1000)", 0, ""), /too many/i, /maxOpsPerScript/],
  ])("%s", (_n, err, text, fix) => {
    const r = explain(err);
    expect(r.text).toMatch(text);
    expect(r.fix).toMatch(fix);
  });

  it("falls back to a generic sentence and keeps the raw message in the fix", () => {
    const r = explain(e("parse", "something nobody planned for"));
    expect(r.text).toMatch(/could not be handled/i);
    expect(r.fix).toContain("something nobody planned for");
  });
});
