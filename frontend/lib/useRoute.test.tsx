import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useRoute } from "./useRoute";

describe("useRoute", () => {
  beforeEach(() => { history.replaceState(null, "", "/"); });

  it("starts from the address bar", () => {
    history.replaceState(null, "", "/duplicates");
    const { result } = renderHook(() => useRoute());
    expect(result.current[0]).toEqual({ view: "duplicates" });
  });

  it("navigating changes the address and the route", () => {
    const { result } = renderHook(() => useRoute());
    act(() => result.current[1]({ view: "settings" }));
    expect(location.pathname).toBe("/settings");
    expect(result.current[0]).toEqual({ view: "settings" });
  });

  it("replace does not add a history entry", () => {
    const { result } = renderHook(() => useRoute());
    const before = history.length;
    act(() => result.current[1]({ view: "connect" }, { replace: true }));
    expect(history.length).toBe(before);
    expect(location.pathname).toBe("/connect");
  });

  it("navigating to the current place adds nothing", () => {
    const { result } = renderHook(() => useRoute());
    act(() => result.current[1]({ view: "settings" }));
    const before = history.length;
    act(() => result.current[1]({ view: "settings" }));
    expect(history.length).toBe(before);
  });

  it("follows the back button", () => {
    const { result } = renderHook(() => useRoute());
    act(() => result.current[1]({ view: "settings" }));
    act(() => { history.replaceState(null, "", "/scripts"); dispatchEvent(new PopStateEvent("popstate")); });
    expect(result.current[0]).toEqual({ view: "scripts" });
  });
});
