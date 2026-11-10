import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MAX_TOASTS, TOAST_MS, ToastProvider, useToast } from "./Toast";

let api: ReturnType<typeof useToast>;
function Grab() { api = useToast(); return null; }
const mount = () => render(<ToastProvider><Grab /></ToastProvider>);

describe("toasts", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("announces a success politely and an error assertively", () => {
    mount();
    act(() => { api.success("Saved"); api.error("Could not save"); });
    expect(screen.getByRole("status")).toHaveTextContent("Saved");
    expect(screen.getByRole("alert")).toHaveTextContent("Could not save");
  });

  it("removes a success after five seconds", () => {
    mount();
    act(() => api.success("Saved"));
    act(() => { vi.advanceTimersByTime(TOAST_MS - 1); });
    expect(screen.getByText("Saved")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1); });
    expect(screen.queryByText("Saved")).toBeNull();
  });

  it("keeps an error until it is dismissed", () => {
    mount();
    act(() => api.error("Could not save"));
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(screen.getByText("Could not save")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("Could not save")).toBeNull();
  });

  it("shows at most three at once, keeping the newest", () => {
    mount();
    act(() => { for (let i = 1; i <= 10; i++) api.info(`Change #${i}`); });
    const shown = screen.getAllByRole("status").map((n) => n.textContent);
    expect(shown).toHaveLength(MAX_TOASTS);
    expect(shown.join(" ")).toContain("Change #8");
    expect(shown.join(" ")).toContain("Change #10");
    expect(shown.join(" ")).not.toContain("Change #7");
  });

  it("runs an action and then goes away", () => {
    mount();
    const onClick = vi.fn();
    act(() => api.info("Change #9 needs your review", { action: { label: "Review", onClick } }));
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Change #9 needs your review")).toBeNull();
  });
});

it("is safe to call with no provider", () => {
  render(<Grab />);
  expect(() => api.success("Saved")).not.toThrow();
});
