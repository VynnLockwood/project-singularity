import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Cosmos } from "./Cosmos";
import type { Intensity } from "@/lib/cosmos";

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

  it("never schedules an animation frame at intensity reduced", async () => {
    motion(false);
    const { raf, unmount } = await mount("reduced");
    expect(raf).not.toHaveBeenCalled();
    unmount();
  });

  it("animates at intensity intense", async () => {
    motion(false);
    const { raf, unmount } = await mount("intense");
    expect(raf).toHaveBeenCalled();
    unmount();
  });

  it("forces reduced when the system asks for reduced motion, whatever the pick is", async () => {
    motion(true);
    const { raf, unmount } = await mount("intense");
    expect(raf).not.toHaveBeenCalled();
    unmount();
  });
});
