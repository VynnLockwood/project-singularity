import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Cosmos } from "./Cosmos";
import { LEVELS, readIntensity, type Intensity } from "@/lib/cosmos";

/** jsdom has no 2D context; without one Cosmos bails out before it can animate, which would make
 *  every "no rAF" assertion pass for the wrong reason. Stub enough of one to reach the drawing. */
function stubCanvas() {
  const grad = { addColorStop: () => {} };
  const ctx = new Proxy({} as Record<string, unknown>, {
    get: (t, k) => (typeof k === "string" && k in t ? t[k] : () => (String(k).startsWith("create") ? grad : undefined)),
    set: (t, k, v) => { t[String(k)] = v; return true; },
  });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(ctx as unknown as CanvasRenderingContext2D);
  return vi.fn(() => 0);
}

const motion = (reduce: boolean) =>
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: reduce && q.includes("reduced-motion"), media: q, addEventListener: () => {}, removeEventListener: () => {} }));

/** Mounts the scene and resolves once loadCosmos has delivered the particles. */
async function mount(intensity?: Intensity) {
  const raf = stubCanvas();
  vi.stubGlobal("requestAnimationFrame", raf);
  const view = render(<Cosmos intensity={intensity} />);
  await waitFor(() => expect(vi.mocked(HTMLCanvasElement.prototype.getContext).mock.calls.length).toBeGreaterThan(0));
  await new Promise((r) => setTimeout(r, 0)); // let loadCosmos' promise settle
  return { ...view, raf };
}

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

describe("Cosmos background", () => {
  it("renders a decorative canvas and does not crash without a 2D context (jsdom)", () => {
    const { container, unmount } = render(<Cosmos />);
    const c = container.querySelector("canvas.cosmos");
    expect(c).not.toBeNull();
    expect(c).toHaveAttribute("aria-hidden", "true");
    unmount();
  });

  it("never schedules an animation frame at intensity still", async () => {
    motion(false);
    const { raf, unmount } = await mount("still");
    expect(raf).not.toHaveBeenCalled();
    unmount();
  });

  it("animates at intensity steady", async () => {
    motion(false);
    const { raf, unmount } = await mount("steady");
    expect(raf).toHaveBeenCalled();
    unmount();
  });

  it("forces still when the system asks for reduced motion, whatever the pick is", async () => {
    motion(true);
    const { raf, unmount } = await mount("supernova");
    expect(raf).not.toHaveBeenCalled();
    unmount();
  });

  it("offers five tiers including two above steady", async () => {
    expect(Object.keys(LEVELS)).toEqual(["still", "subtle", "steady", "vivid", "supernova"]);
  });

  it("migrates a stored legacy level instead of resetting it", async () => {
    const migrated = { reduced: "still", calm: "subtle", intense: "steady" };
    for (const [old, expected] of Object.entries(migrated)) {
      localStorage.setItem("singularity.cosmos_intensity", old);
      expect(readIntensity()).toBe(expected);
    }
  });
});
