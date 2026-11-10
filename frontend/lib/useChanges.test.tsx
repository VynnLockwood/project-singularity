import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Change, Me } from "./api";
import type { ServerEvent } from "./events";

let emit: (e: ServerEvent) => void = () => {};
let setOnline: (open: boolean) => void = () => {};
const getQueue = vi.fn();
const decide = vi.fn();

vi.mock("./events", () => ({
  connectEvents: (onEvent: (e: ServerEvent) => void, onState?: (open: boolean) => void) => {
    emit = onEvent; setOnline = onState ?? (() => {});
    return () => {};
  },
}));
vi.mock("./api", async (orig) => ({ ...(await orig<typeof import("./api")>()), getQueue: () => getQueue(), decide: (...a: unknown[]) => decide(...a) }));

import { ApiError } from "./api";
import { activityLevel, useChanges } from "./useChanges";

const me: Me = { id: 1, email: "ann@example.com", name: "", role: "approver" };
const change = (id: number, over: Partial<Change> = {}): Change => ({
  id, status: "pending", kind: "schema", source: "mcp", requester: "bob@example.com", prompt: "", createdAt: "",
  decidedBy: null, error: "", decisionNote: "", tables: [], diff: [], check: {}, ...over,
});

describe("useChanges", () => {
  beforeEach(() => { getQueue.mockReset().mockResolvedValue([]); decide.mockReset(); });

  it("loads what is waiting for this person", async () => {
    getQueue.mockResolvedValue([change(3), change(2)]);
    const { result } = renderHook(() => useChanges(me));
    await waitFor(() => expect(result.current.mine.map((c) => c.id)).toEqual([2, 3]));
  });

  it("adds a change from the event stream and tells the caller it is new", async () => {
    const onEvent = vi.fn();
    const { result } = renderHook(() => useChanges(me, onEvent));
    await waitFor(() => expect(getQueue).toHaveBeenCalled());
    act(() => emit({ type: "change", change: change(9) }));
    expect(result.current.mine.map((c) => c.id)).toEqual([9]);
    expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ id: 9 }), undefined);
  });

  it("drops a change from the waiting list once it is applied, passing the old copy along", async () => {
    getQueue.mockResolvedValue([change(4)]);
    const onEvent = vi.fn();
    const { result } = renderHook(() => useChanges(me, onEvent));
    await waitFor(() => expect(result.current.mine).toHaveLength(1));
    const before = result.current.schemaKey;
    act(() => emit({ type: "change", change: change(4, { status: "applied" }) }));
    expect(result.current.mine).toHaveLength(0);
    expect(result.current.live.find((c) => c.id === 4)?.status).toBe("applied");
    expect(result.current.schemaKey).toBe(before + 1);
    expect(onEvent).toHaveBeenLastCalledWith(expect.objectContaining({ status: "applied" }), expect.objectContaining({ status: "pending" }));
  });

  it("does not bump the schema key for an applied data change", async () => {
    const { result } = renderHook(() => useChanges(me));
    await waitFor(() => expect(getQueue).toHaveBeenCalled());
    const before = result.current.schemaKey;
    act(() => emit({ type: "change", change: change(5, { kind: "data", status: "applied" }) }));
    expect(result.current.schemaKey).toBe(before);
  });

  it("marks only the change being decided while the request is in flight", async () => {
    getQueue.mockResolvedValue([change(6)]);
    let finish: (c: Change) => void = () => {};
    decide.mockReturnValue(new Promise<Change>((r) => { finish = r; }));
    const { result } = renderHook(() => useChanges(me));
    await waitFor(() => expect(result.current.mine).toHaveLength(1));
    let done: Promise<void>;
    act(() => { done = result.current.decide(result.current.mine[0], "approve"); });
    expect(result.current.deciding).toBe(6);
    await act(async () => { finish(change(6, { status: "applied" })); await done; });
    expect(result.current.deciding).toBeNull();
    expect(result.current.mine).toHaveLength(0);
  });

  it("keeps the server's reason when a decision fails", async () => {
    getQueue.mockResolvedValue([change(7)]);
    decide.mockRejectedValue(new ApiError(409, "This change was already decided."));
    const { result } = renderHook(() => useChanges(me));
    await waitFor(() => expect(result.current.mine).toHaveLength(1));
    await act(async () => { await result.current.decide(result.current.mine[0], "approve"); });
    expect(result.current.error).toBe("This change was already decided.");
    expect(result.current.deciding).toBeNull();
  });

  it("adds a change proposed from this browser", async () => {
    const { result } = renderHook(() => useChanges(me));
    await waitFor(() => expect(getQueue).toHaveBeenCalled());
    act(() => result.current.add(change(11)));
    expect(result.current.mine.map((c) => c.id)).toEqual([11]);
  });

  it("reports the live connection state", async () => {
    const { result } = renderHook(() => useChanges(me));
    expect(result.current.online).toBe(false);
    act(() => setOnline(true));
    expect(result.current.online).toBe(true);
  });
});

describe("activityLevel", () => {
  it("is 0 with nothing waiting and reaches 1 at five", () => {
    expect(activityLevel(0)).toBe(0);
    expect(activityLevel(2)).toBeCloseTo(0.4);
    expect(activityLevel(5)).toBe(1);
    expect(activityLevel(9)).toBe(1);
  });
});
