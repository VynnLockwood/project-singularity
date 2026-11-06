import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Me } from "@/lib/api";

const me = (role: Me["role"]): Me => ({ id: 1, email: "a@b.c", name: "", role });

async function setup(role: Me["role"]) {
  vi.resetModules();
  const fns = {
    listTokens: vi.fn().mockResolvedValue([{ id: 3, name: "old", prefix: "sgl_abcdef", createdAt: "x", lastUsedAt: null }]),
    createToken: vi.fn().mockResolvedValue({ id: 4, name: "Claude", prefix: "sgl_zzzzzz", token: "sgl_secret123" }),
    revokeToken: vi.fn().mockResolvedValue({ ok: true }),
  };
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), ...fns }));
  const { ConnectView } = await import("./ConnectView");
  render(<ConnectView me={me(role)} />);
  return fns;
}

describe("ConnectView", () => {
  it("creates a link, shows it once with the Gemini settings, and lists connections", async () => {
    const { createToken } = await setup("editor");
    expect(await screen.findByText("old")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Connection name"), { target: { value: "Claude" } });
    fireEvent.click(screen.getByText("Create connection link"));
    await waitFor(() => expect(createToken).toHaveBeenCalledWith("Claude"));
    expect((await screen.findByTestId("link")).textContent).toMatch(/\/mcp\/t\/sgl_secret123$/);
    expect(screen.getByText(/"httpUrl"/)).toBeInTheDocument();
  });

  it("revokes a connection", async () => {
    const { revokeToken } = await setup("approver");
    fireEvent.click(await screen.findByLabelText("Revoke old"));
    await waitFor(() => expect(revokeToken).toHaveBeenCalledWith(3));
  });

  it("is closed to viewers", async () => {
    const { listTokens } = await setup("viewer");
    expect(screen.getByText(/Viewers cannot connect/)).toBeInTheDocument();
    expect(listTokens).not.toHaveBeenCalled();
  });
});
