import { afterEach, describe, expect, it, vi } from "vitest";
import { ACTIVITY_EVENT, FLARE_EVENT, ease, flare, setActivity } from "./activity";

describe("ease", () => {
  it("does not move in no time", () => expect(ease(0, 1, 0)).toBe(0));
  it("covers about 63% of the gap in one time constant", () => expect(ease(0, 1, 2)).toBeCloseTo(0.632, 2));
  it("never overshoots, in either direction", () => {
    expect(ease(0, 1, 1000)).toBeLessThanOrEqual(1);
    expect(ease(1, 0, 1000)).toBeGreaterThanOrEqual(0);
    expect(ease(0.5, 0.5, 3)).toBe(0.5);
  });
});

describe("activity events", () => {
  const seen: number[] = [];
  const onActivity = (e: Event) => seen.push((e as CustomEvent<number>).detail);
  afterEach(() => { removeEventListener(ACTIVITY_EVENT, onActivity); seen.length = 0; });

  it("clamps the level to 0..1", () => {
    addEventListener(ACTIVITY_EVENT, onActivity);
    setActivity(3); setActivity(-1); setActivity(0.4);
    expect(seen).toEqual([1, 0, 0.4]);
  });
  it("announces a flare", () => {
    const onFlare = vi.fn();
    addEventListener(FLARE_EVENT, onFlare);
    flare();
    removeEventListener(FLARE_EVENT, onFlare);
    expect(onFlare).toHaveBeenCalledTimes(1);
  });
});
