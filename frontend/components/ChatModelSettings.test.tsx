import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { SettingsPayload, Tunable } from "@/lib/api";

const base = { min: null, max: null, help: "h", overridden: false };
const provider: Tunable = { ...base, key: "chat.provider", label: "Chat provider", kind: "choice", default: "openrouter", value: "openrouter", options: ["openrouter", "opencode"] };
const models: Tunable = { ...base, key: "chat.models", label: "Chat models", kind: "models", default: ["a/b", "c/d"], value: ["a/b", "c/d"] };
const defaultModel: Tunable = { ...base, key: "chat.defaultModel", label: "Default chat model", kind: "model", default: "a/b", value: "a/b", options: ["a/b", "c/d"] };
const keySet = { openrouter: true, opencode: false };

async function setup(canEdit = true, saveImpl?: () => Promise<SettingsPayload>) {
  vi.resetModules();
  const saveSettings = vi.fn().mockImplementation(saveImpl ?? (async () => ({ settings: [], keySet })));
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), saveSettings }));
  const { ChatModelSettings } = await import("./ChatModelSettings");
  const onSaved = vi.fn();
  render(<ChatModelSettings provider={provider} models={models} defaultModel={defaultModel} keySet={keySet} canEdit={canEdit} onSaved={onSaved} />);
  return { saveSettings, onSaved };
}
const add = (v: string) => {
  fireEvent.change(screen.getByLabelText("Add chat model"), { target: { value: v } });
  fireEvent.click(screen.getByText("Add"));
};

describe("ChatModelSettings", () => {
  it("offers exactly the two providers, shows key badges, saves the provider", async () => {
    const { saveSettings, onSaved } = await setup();
    const sel = screen.getByLabelText("Chat provider") as HTMLSelectElement;
    expect([...sel.options].map((o) => o.value)).toEqual(["openrouter", "opencode"]);
    expect(screen.getByLabelText("openrouter API key")).toHaveTextContent("openrouter: set");
    expect(screen.getByLabelText("opencode API key")).toHaveTextContent("opencode: missing");
    expect(screen.getByText("Save")).toBeDisabled();
    fireEvent.change(sel, { target: { value: "opencode" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(saveSettings).toHaveBeenCalledWith({ "chat.provider": "opencode" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  it("adds a model and saves the whole list", async () => {
    const { saveSettings } = await setup();
    add("new/model");
    expect(screen.getByText("new/model")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(saveSettings).toHaveBeenCalledWith({ "chat.models": ["a/b", "c/d", "new/model"] }));
  });

  it("rejects duplicates and ids with spaces inline and keeps Save disabled", async () => {
    await setup();
    add("a/b");
    expect(screen.getByText(/already in the list/i)).toBeInTheDocument();
    expect(screen.getByText("Save")).toBeDisabled();
    add("bad id");
    expect(screen.getByText(/letters, digits/i)).toBeInTheDocument();
    expect(screen.getByText("Save")).toBeDisabled();
  });

  it("blocks removing the current default", async () => {
    await setup();
    const rm = screen.getByLabelText("Remove a/b");
    expect(rm).toBeDisabled();
    expect(rm).toHaveAttribute("title", "Pick another default first");
    expect(screen.getByLabelText("Remove c/d")).not.toBeDisabled();
  });

  it("sends models and the new default in ONE call", async () => {
    const { saveSettings } = await setup();
    add("x/y");
    fireEvent.click(screen.getByLabelText("Use x/y as default"));
    fireEvent.click(screen.getByLabelText("Remove a/b"));
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(saveSettings).toHaveBeenCalledTimes(1));
    expect(saveSettings).toHaveBeenCalledWith({ "chat.models": ["c/d", "x/y"], "chat.defaultModel": "x/y" });
  });

  it("saves only the default when just the default changes", async () => {
    const { saveSettings } = await setup();
    fireEvent.click(screen.getByLabelText("Use c/d as default"));
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(saveSettings).toHaveBeenCalledWith({ "chat.defaultModel": "c/d" }));
  });

  it("disables everything for non-approvers", async () => {
    await setup(false);
    expect(screen.getByLabelText("Add chat model")).toBeDisabled();
    expect(screen.getByText("Add")).toBeDisabled();
    expect(screen.getByLabelText("Chat provider")).toBeDisabled();
    expect(screen.getByLabelText("Remove c/d")).toBeDisabled();
    expect(screen.getByLabelText("Use c/d as default")).toBeDisabled();
    expect(screen.getByText("Only approvers can change these.")).toBeInTheDocument();
  });

  it("shows a server error in an alert", async () => {
    await setup(true, async () => { const { ApiError } = await import("@/lib/api"); throw new ApiError(400, "Default chat model: choose one of the allowed models"); });
    add("n/m");
    fireEvent.click(screen.getByText("Save"));
    expect(await screen.findByRole("alert")).toHaveTextContent("choose one of the allowed models");
  });
});
