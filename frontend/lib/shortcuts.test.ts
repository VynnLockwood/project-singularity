import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isTyping, useShortcut } from "./shortcuts";

const key = (k: string, init: KeyboardEventInit = {}, target: EventTarget = document.body) =>
  target.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, ...init }));

describe("isTyping", () => {
  it("is true inside text controls and false elsewhere", () => {
    for (const tag of ["input", "textarea", "select"]) expect(isTyping(document.createElement(tag))).toBe(true);
    const div = document.createElement("div");
    div.setAttribute("contenteditable", "true");
    expect(isTyping(div)).toBe(true);
    expect(isTyping(document.createElement("button"))).toBe(false);
    expect(isTyping(null)).toBe(false);
  });
});

describe("useShortcut", () => {
  let area: HTMLTextAreaElement;
  beforeEach(() => { vi.useFakeTimers(); area = document.body.appendChild(document.createElement("textarea")); });
  afterEach(() => { vi.useRealTimers(); area.remove(); });

  it("fires a two-key sequence in order", () => {
    const run = vi.fn();
    renderHook(() => useShortcut("g r", run));
    key("r");
    expect(run).not.toHaveBeenCalled();
    key("g"); key("r");
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("forgets the first key after a second", () => {
    const run = vi.fn();
    renderHook(() => useShortcut("g r", run));
    key("g");
    vi.advanceTimersByTime(1001);
    key("r");
    expect(run).not.toHaveBeenCalled();
  });

  it("never fires a sequence while the person is typing", () => {
    const run = vi.fn();
    renderHook(() => useShortcut("g r", run));
    key("g", {}, area); key("r", {}, area);
    expect(run).not.toHaveBeenCalled();
  });

  it("does not treat a held modifier as part of a sequence", () => {
    const run = vi.fn();
    renderHook(() => useShortcut("g r", run));
    key("g"); key("r", { ctrlKey: true });
    expect(run).not.toHaveBeenCalled();
  });

  it("fires a mod chord with Ctrl or Meta, even while typing", () => {
    const run = vi.fn();
    renderHook(() => useShortcut("mod+k", run));
    key("k");
    expect(run).not.toHaveBeenCalled();
    key("k", { ctrlKey: true });
    key("K", { metaKey: true }, area);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("stops listening when unmounted", () => {
    const run = vi.fn();
    const { unmount } = renderHook(() => useShortcut("mod+k", run));
    unmount();
    key("k", { ctrlKey: true });
    expect(run).not.toHaveBeenCalled();
  });
});
