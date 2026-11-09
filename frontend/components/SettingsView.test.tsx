import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Me, Rule, Tunable } from "@/lib/api";

const me = (role: Me["role"]): Me => ({ id: 1, email: "ap@x.com", name: "", role });
const tunables: Tunable[] = [
  { key: "search.minConfidence", label: "Search confidence", help: "h", kind: "float", min: 0, max: 1, default: 0.8, value: 0.8, overridden: false },
  { key: "search.topK", label: "Search results", help: "h", kind: "int", min: 1, max: 20, default: 5, value: 9, overridden: true },
  { key: "chat.provider", label: "Chat provider", help: "h", kind: "choice", min: null, max: null, default: "openrouter", value: "openrouter", overridden: false, options: ["openrouter", "opencode"] },
  { key: "chat.models", label: "Chat models", help: "h", kind: "models", min: null, max: null, default: ["a/b", "c/d"], value: ["a/b", "c/d"], overridden: false },
  { key: "chat.defaultModel", label: "Default chat model", help: "h", kind: "model", min: null, max: null, default: "a/b", value: "a/b", overridden: false, options: ["a/b", "c/d"] },
];
const rule: Rule = { id: 4, name: "noFloat", type: "forbidden_type", params: { types: ["real"] }, severity: "block", enabled: true, description: "No floats.", version: 2 };

const keySet = { openrouter: true, opencode: false };

async function setup(role: Me["role"]) {
  vi.resetModules();
  const fns = {
    getSettings: vi.fn().mockResolvedValue({ settings: tunables, keySet }),
    saveSettings: vi.fn().mockImplementation(async (c: Record<string, unknown>) =>
      ({ settings: tunables.map((t) => (t.key in c ? { ...t, value: c[t.key] ?? t.default, overridden: c[t.key] !== null } : t)), keySet })),
    listRules: vi.fn().mockResolvedValue([rule]),
    updateRule: vi.fn().mockResolvedValue(rule),
    ruleVersions: vi.fn().mockResolvedValue([{ version: 2, action: "updated", changedBy: "x@y.z", createdAt: "2026-01-01T00:00:00Z" }, { version: 1, action: "created", changedBy: null, createdAt: "2026-01-01T00:00:00Z" }]),
    rollbackRule: vi.fn().mockResolvedValue(rule),
    listUsers: vi.fn().mockResolvedValue([me("approver"), { id: 2, email: "ed@x.com", name: "", role: "viewer" }]),
    setUserRole: vi.fn().mockResolvedValue({}),
  };
  vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), ...fns }));
  const { SettingsView } = await import("./SettingsView");
  render(<SettingsView me={me(role)} />);
  return fns;
}

describe("SettingsView", () => {
  it("lets an approver change a quality setting and save only what changed", async () => {
    const { saveSettings } = await setup("approver");
    fireEvent.change(await screen.findByLabelText("Search confidence"), { target: { value: "0.7" } });
    fireEvent.click(screen.getByText("Save changes"));
    await waitFor(() => expect(saveSettings).toHaveBeenCalledWith({ "search.minConfidence": 0.7 }));
    expect(await screen.findByText(/Saved/)).toBeInTheDocument();
  });

  it("resets an overridden setting to the file default", async () => {
    const { saveSettings } = await setup("approver");
    fireEvent.click(await screen.findByLabelText("Reset Search results"));
    await waitFor(() => expect(saveSettings).toHaveBeenCalledWith({ "search.topK": null }));
  });

  it("picks the default model from the allowed list", async () => {
    const { saveSettings } = await setup("approver");
    fireEvent.click(await screen.findByLabelText("Use c/d as default"));
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(saveSettings).toHaveBeenCalledWith({ "chat.defaultModel": "c/d" }));
  });

  it("lets an approver pick a provider and saves it as a string", async () => {
    const { saveSettings } = await setup("approver");
    fireEvent.change(await screen.findByLabelText("Chat provider"), { target: { value: "opencode" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(saveSettings).toHaveBeenCalledWith({ "chat.provider": "opencode" }));
    expect(await screen.findByText(/Saved/)).toBeInTheDocument();
  });

  it("shows only whether a provider key is set, never the value", async () => {
    await setup("approver");
    expect(await screen.findByLabelText("openrouter API key")).toHaveTextContent("set");
    expect(screen.getByLabelText("opencode API key")).toHaveTextContent("missing");
  });

  it("is read-only for editors and hides the Team section", async () => {
    await setup("editor");
    expect(await screen.findByLabelText("Search confidence")).toBeDisabled();
    expect(await screen.findByLabelText("Chat provider")).toBeDisabled();
    expect(screen.getByLabelText("Add chat model")).toBeDisabled();
    expect(screen.queryByText("Save changes")).toBeNull();
    expect(screen.queryByText("Team")).toBeNull();
  });

  it("edits a rule: severity, enabled, parameters (JSON checked), history and restore", async () => {
    const { updateRule, rollbackRule } = await setup("approver");
    fireEvent.click(screen.getByText("Rules"));
    fireEvent.change(await screen.findByLabelText("noFloat severity"), { target: { value: "warn" } });
    await waitFor(() => expect(updateRule).toHaveBeenCalledWith(4, { severity: "warn" }));
    fireEvent.click(screen.getByLabelText("noFloat enabled"));
    await waitFor(() => expect(updateRule).toHaveBeenCalledWith(4, { enabled: false }));

    fireEvent.click(screen.getByText("Details"));
    const box = screen.getByLabelText("noFloat parameters");
    fireEvent.change(box, { target: { value: "{not json" } });
    fireEvent.click(screen.getByText("Save parameters"));
    expect(await screen.findByText(/not valid JSON/)).toBeInTheDocument();
    fireEvent.change(box, { target: { value: '{"types":["real","double precision"]}' } });
    fireEvent.click(screen.getByText("Save parameters"));
    await waitFor(() => expect(updateRule).toHaveBeenCalledWith(4, { params: { types: ["real", "double precision"] } }));

    fireEvent.click(screen.getByText("History"));
    const hist = await screen.findByLabelText("noFloat history");
    fireEvent.click(within(hist).getByText("Restore this version"));
    await waitFor(() => expect(rollbackRule).toHaveBeenCalledWith(4, 1));
  });

  it("changes a teammate's role but not your own", async () => {
    const { setUserRole } = await setup("approver");
    fireEvent.click(screen.getByText("Team"));
    expect(await screen.findByLabelText("Role of ap@x.com")).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Role of ed@x.com"), { target: { value: "editor" } });
    await waitFor(() => expect(setUserRole).toHaveBeenCalledWith(2, "editor"));
  });
});

describe("SettingsView waiting changes", () => {
  it("tells people that AI-proposed settings changes wait for an approver, and links to the queue", async () => {
    vi.resetModules();
    vi.doMock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), getSettings: vi.fn().mockResolvedValue({ settings: [], keySet: { openrouter: false, opencode: false } }), listRules: vi.fn().mockResolvedValue([]) }));
    const { SettingsView } = await import("./SettingsView");
    const open = vi.fn();
    render(<SettingsView me={me("approver")} waiting={2} onOpenQueue={open} />);
    expect(screen.getByRole("status")).toHaveTextContent("2 settings, rules or roles changes are waiting");
    fireEvent.click(screen.getByText("Review in the Queue"));
    expect(open).toHaveBeenCalled();
  });
});
