import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePersistentFlag } from "./storage";

describe("usePersistentFlag", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it("uses initial when nothing stored or garbage stored", () => {
    const { result } = renderHook(() => usePersistentFlag("k", true));
    expect(result.current[0]).toBe(true);
    localStorage.setItem("k2", "maybe");
    const r2 = renderHook(() => usePersistentFlag("k2", false));
    expect(r2.result.current[0]).toBe(false);
  });
  it("reads stored values", () => {
    localStorage.setItem("a", "1");
    localStorage.setItem("b", "0");
    expect(renderHook(() => usePersistentFlag("a", false)).result.current[0]).toBe(true);
    expect(renderHook(() => usePersistentFlag("b", true)).result.current[0]).toBe(false);
  });
  it("writes on set", () => {
    const { result } = renderHook(() => usePersistentFlag("k", false));
    act(() => result.current[1](true));
    expect(result.current[0]).toBe(true);
    expect(localStorage.getItem("k")).toBe("1");
    act(() => result.current[1](false));
    expect(localStorage.getItem("k")).toBe("0");
  });
  it("survives storage that throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
    const { result } = renderHook(() => usePersistentFlag("k", true));
    expect(result.current[0]).toBe(true);
    act(() => result.current[1](false));
    expect(result.current[0]).toBe(false);
  });
});
