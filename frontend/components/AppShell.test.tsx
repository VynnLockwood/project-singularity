import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Me } from "@/lib/api";

const me: Me = { id: 1, email: "ed@example.com", name: "", role: "approver" };

describe("AppShell against the real API response shapes", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.resetModules(); });

  it("loads the pending queue from {changes: [...]} (what /api/queue returns) without crashing", async () => {
    vi.resetModules();
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      const body = String(url).endsWith("/queue") ? { changes: [] } : String(url).includes("/chat/") ? { conversations: [] } : {};
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }));
    const { AppShell } = await import("./AppShell");
    render(<AppShell me={me} onLogout={() => {}} />);
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/queue"), expect.anything()));
    expect(screen.getByText("Duplicates")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Database" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Schema" })).toBeNull();
  });

  it("getQueue returns the changes array, or [] when the field is missing", async () => {
    vi.resetModules();
    const ch = { id: 5 };
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ changes: [ch] }), { status: 200 }))
      .mockResolvedValueOnce(new Response("{}", { status: 200 })));
    const { getQueue } = await import("@/lib/api");
    expect(await getQueue()).toEqual([ch]);
    expect(await getQueue()).toEqual([]);
  });
});
