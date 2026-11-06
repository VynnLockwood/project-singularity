import { describe, expect, it } from "vitest";
import { snippets } from "./connect";

describe("snippets", () => {
  const s = snippets("https://singularity.example.com/", "sgl_abc");
  it("builds the link and endpoint from the site address", () => {
    expect(s.endpoint).toBe("https://singularity.example.com/mcp");
    expect(s.link).toBe("https://singularity.example.com/mcp/t/sgl_abc");
  });
  it("gives Gemini valid settings JSON and Claude Code a header command", () => {
    expect(JSON.parse(s.gemini).mcpServers.singularity).toEqual({
      httpUrl: "https://singularity.example.com/mcp", headers: { Authorization: "Bearer sgl_abc" },
    });
    expect(s.claudeCode).toContain('--header "Authorization: Bearer sgl_abc"');
  });
});
