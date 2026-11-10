import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Me } from "@/lib/api";
import { change } from "@/lib/fixtures";
import type { ServerEvent } from "@/lib/events";

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

describe("AppShell navigation and review", () => {
  let emit: (e: ServerEvent) => void = () => {};

  async function mount(role: Me["role"] = "approver", path = "/") {
    vi.resetModules();
    history.replaceState(null, "", path);
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      const u = String(url);
      const body = u.endsWith("/queue") ? { changes: [] } : u.includes("/chat/") ? { conversations: [] }
        : u.includes("/schema/schemas") ? { schemas: [] } : u.includes("/schema/graph") ? { tables: [], relations: [] } : {};
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }));
    vi.doMock("@/lib/events", () => ({
      connectEvents: (onEvent: (e: ServerEvent) => void, onState?: (open: boolean) => void) => { emit = onEvent; onState?.(true); return () => {}; },
    }));
    const { AppShell } = await import("./AppShell");
    render(<AppShell me={{ ...me, role }} onLogout={() => {}} />);
    await waitFor(() => expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument());
  }
  const nav = () => within(screen.getByRole("navigation", { name: "Main" }));

  beforeEach(() => { history.replaceState(null, "", "/"); });
  afterEach(() => { vi.unstubAllGlobals(); vi.doUnmock("@/lib/events"); vi.resetModules(); });

  it("puts the place in the address bar and marks it in the rail", async () => {
    await mount();
    fireEvent.click(nav().getByRole("button", { name: "Settings" }));
    expect(location.pathname).toBe("/settings");
    expect(nav().getByRole("button", { name: "Settings" })).toHaveAttribute("aria-current", "page");
    expect(nav().getByRole("button", { name: "Chat" })).not.toHaveAttribute("aria-current");
  });

  it("opens the view a link points at", async () => {
    await mount("approver", "/duplicates");
    expect(nav().getByRole("button", { name: "Duplicates" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("main")).toBeInTheDocument();
  });

  it("says so when the address is not a place, and offers a way back", async () => {
    await mount("approver", "/nope");
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Go to Chat" }));
    expect(location.pathname).toBe("/");
  });

  it("announces a new change with a toast instead of covering the page", async () => {
    await mount();
    act(() => emit({ type: "change", change: change({ id: 9, requester: "bob@example.com", source: "mcp" }) }));
    const toast = await screen.findByText("Change #9 needs your review");
    expect(toast).toBeInTheDocument();
    expect(screen.queryByRole("complementary", { name: "Needs review" })).toBeNull();
    expect(document.querySelector('[aria-modal="true"]')).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    expect(within(screen.getByRole("complementary", { name: "Needs review" })).getByLabelText("Change #9")).toBeInTheDocument();
  });

  it("counts what is waiting next to Activity without changing its name", async () => {
    await mount();
    act(() => emit({ type: "change", change: change({ id: 9 }) }));
    expect(await screen.findByText("1 waiting")).toBeInTheDocument();
    expect(nav().getByRole("button", { name: "Activity" })).toBeInTheDocument();
  });

  it("tells the requester when their change is applied or rejected", async () => {
    await mount();
    act(() => emit({ type: "change", change: change({ id: 4 }) }));
    act(() => emit({ type: "change", change: change({ id: 4, status: "applied", decidedBy: "x@example.com" }) }));
    expect(await screen.findByText("Change #4 applied")).toBeInTheDocument();
    act(() => emit({ type: "change", change: change({ id: 5 }) }));
    act(() => emit({ type: "change", change: change({ id: 5, status: "rejected" }) }));
    expect(await screen.findByText("Change #5 rejected")).toBeInTheDocument();
  });

  it("tells the background how busy the queue is, and marks the page as the app", async () => {
    const levels: number[] = [];
    const onLevel = (e: Event) => levels.push((e as CustomEvent<number>).detail);
    addEventListener("cosmos:activity", onLevel);
    await mount();
    expect(document.documentElement.dataset.app).toBe("true");
    act(() => emit({ type: "change", change: change({ id: 9 }) }));
    await waitFor(() => expect(levels.at(-1)).toBeCloseTo(0.2));
    removeEventListener("cosmos:activity", onLevel);
  });

  it("opens the review tray with g then r", async () => {
    await mount();
    fireEvent.keyDown(document.body, { key: "g" });
    fireEvent.keyDown(document.body, { key: "r" });
    expect(screen.getByRole("complementary", { name: "Needs review" })).toBeInTheDocument();
  });

  it("opens the command palette with Ctrl+K, even from the message box", async () => {
    await mount();
    const box = await screen.findByLabelText("Message");
    box.focus();
    fireEvent.keyDown(box, { key: "k", ctrlKey: true });
    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull();
  });

  it("opens the command palette from the top bar and jumps to a view", async () => {
    await mount();
    fireEvent.click(screen.getByRole("button", { name: /Search or jump to/ }));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "connect" } });
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
    expect(location.pathname).toBe("/connect");
  });

  it("never blocks the whole app with a working overlay", async () => {
    await mount();
    fireEvent.click(nav().getByRole("button", { name: "Data" }));
    expect(screen.queryByText("Working…")).toBeNull();
    expect(document.querySelector(".overlay.busy")).toBeNull();
  });

  it("hides Scripts from a viewer", async () => {
    await mount("viewer");
    expect(nav().queryByRole("button", { name: "Scripts" })).toBeNull();
    expect(nav().getByRole("button", { name: "Activity" })).toBeInTheDocument();
  });

  it("has a skip link to the content", async () => {
    await mount();
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#content");
    expect(screen.getByRole("main")).toHaveAttribute("id", "content");
  });
});
