import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Me } from "@/lib/api";

const getSchemas = vi.fn();
const getGraph = vi.fn();
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getSchemas: () => getSchemas(),
  getGraph: (s?: string) => getGraph(s),
}));

import { CommandPalette } from "./CommandPalette";

const me = (role: Me["role"]): Me => ({ id: 1, email: "ed@example.com", name: "", role });
const table = (name: string) => ({ name, comment: null, rowEstimate: 0, columns: [] });

function mount(role: Me["role"] = "approver") {
  const navigate = vi.fn();
  const onClose = vi.fn();
  const onLogout = vi.fn();
  render(<CommandPalette me={me(role)} open onClose={onClose} navigate={navigate} refreshKey={0} onLogout={onLogout} />);
  return { navigate, onClose, onLogout, input: screen.getByRole("combobox") };
}
const type = (input: HTMLElement, value: string) => fireEvent.change(input, { target: { value } });

describe("CommandPalette", () => {
  beforeEach(() => {
    getSchemas.mockReset().mockResolvedValue([{ name: "master", tables: 2, comment: null }]);
    getGraph.mockReset().mockResolvedValue({ tables: [table("province"), table("district")], relations: [] });
  });

  it("is a dialog with a combobox and a listbox", () => {
    mount();
    expect(screen.getByRole("dialog", { name: "Command palette" })).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    expect(screen.getByRole("combobox")).toHaveFocus();
  });

  it("jumps to a table in Database, or in Data with Shift", async () => {
    const { navigate, onClose, input } = mount();
    await waitFor(() => expect(getGraph).toHaveBeenCalledWith("master"));
    type(input, "prov");
    expect(await screen.findByRole("option", { name: /master\.province/ })).toBeInTheDocument();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(navigate).toHaveBeenLastCalledWith({ view: "database", schema: "master", table: "province" });
    expect(onClose).toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(navigate).toHaveBeenLastCalledWith({ view: "data", schema: "master", table: "province" });
  });

  it("still offers views and actions when the schema cannot be loaded", async () => {
    getSchemas.mockRejectedValue(new Error("down"));
    const { navigate, input } = mount();
    await waitFor(() => expect(getSchemas).toHaveBeenCalled());
    type(input, "settings");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith({ view: "settings" });
    type(input, "new chat");
    expect(screen.getByRole("option", { name: /New chat/ })).toBeInTheDocument();
  });

  it("moves the selection with the arrow keys and runs a clicked option", () => {
    const { navigate, input } = mount();
    const options = () => screen.getAllByRole("option");
    expect(options()[0]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(options()[1]).toHaveAttribute("aria-selected", "true");
    expect(options()[0]).toHaveAttribute("aria-selected", "false");
    fireEvent.keyDown(input, { key: "ArrowUp" });
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(options()[options().length - 1]).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("option", { name: /Go to Connect/ }));
    expect(navigate).toHaveBeenCalledWith({ view: "connect" });
  });

  it("offers only what the role may do", () => {
    const { input } = mount("viewer");
    type(input, "new");
    expect(screen.queryByRole("option", { name: /New table/ })).toBeNull();
    expect(screen.getByRole("option", { name: /New chat/ })).toBeInTheDocument();
    type(input, "scripts");
    expect(screen.queryByRole("option", { name: /Go to Scripts/ })).toBeNull();
  });

  it("says when nothing matches and closes on Escape", () => {
    const { onClose, input } = mount();
    type(input, "zzzzzz");
    expect(screen.getByText("No matches")).toBeInTheDocument();
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("signs out from the palette", () => {
    const { onLogout, input } = mount();
    type(input, "sign out");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onLogout).toHaveBeenCalled();
  });
});
