/** How the app tells the background canvas what is going on, without the two knowing each other. */
export const ACTIVITY_EVENT = "cosmos:activity";
export const FLARE_EVENT = "cosmos:flare";

/** Set how busy the review queue is, 0 (empty) to 1. The disk spins up and brightens with it. */
export function setActivity(level: number): void {
  if (typeof dispatchEvent === "undefined") return;
  dispatchEvent(new CustomEvent<number>(ACTIVITY_EVENT, { detail: Math.max(0, Math.min(1, level)) }));
}

/** One short brightening of the inner ring: a change was applied. */
export function flare(): void {
  if (typeof dispatchEvent !== "undefined") dispatchEvent(new Event(FLARE_EVENT));
}

/** Move `current` towards `target` exponentially, so the scene never jumps. `tau` is the time constant in seconds. */
export function ease(current: number, target: number, dtSeconds: number, tau = 2): number {
  if (dtSeconds <= 0) return current;
  return target + (current - target) * Math.exp(-dtSeconds / tau);
}
