import { loadWasm } from "./stats";

export interface Star { x: number; y: number; size: number; alpha: number; warm: number }
export interface Particle { angle: number; radius: number; speed: number; size: number; alpha: number; hue: number }
export interface Cosmos { stars: Star[]; disk: Particle[] }

/** Background intensity, five ascending tiers; `still` is also forced when the OS asks for reduced motion.
 *  ponytail: particle/star counts are hand-tuned; raise the top two only after measuring on a low-end device. */
export const LEVELS = {
  still:     { particles: 0,     stars: 160, streak: 0,   glow: 0 },
  subtle:    { particles: 700,   stars: 160, streak: 1,   glow: 1 },
  steady:    { particles: 3200,  stars: 320, streak: 2.2, glow: 2 },
  vivid:     { particles: 6000,  stars: 480, streak: 3.0, glow: 3 },
  supernova: { particles: 10000, stars: 700, streak: 4.0, glow: 4 },
} as const;
export type Intensity = keyof typeof LEVELS;

const KEY = "singularity.cosmos_intensity";
/** Fired when the pick changes so the open scene rebuilds; `storage` covers the other tabs. */
export const INTENSITY_EVENT = "cosmos:intensity";

/** Old names shipped before the five-tier rename; map them so nobody loses their pick. */
const LEGACY: Record<string, Intensity> = { reduced: "still", calm: "subtle", intense: "steady" };

/** This device's choice, kept per browser — the background is a personal preference, not a shared knob. */
export function readIntensity(): Intensity {
  const v = typeof localStorage === "undefined" ? null : localStorage.getItem(KEY);
  if (v && v in LEVELS) return v as Intensity;
  if (v && v in LEGACY) return LEGACY[v];
  return "steady";
}
export function writeIntensity(level: Intensity) {
  if (typeof localStorage !== "undefined") localStorage.setItem(KEY, level);
}

const TAU = 6.283185307179586;

/** TypeScript twin of engine/src/cosmos.rs: same xorshift32 and arithmetic, same golden cases. */
export function cosmos(seed = 1, stars = 220, particles = 1600): Cosmos {
  let x = (seed >>> 0) === 0 ? 0x9e3779b9 : seed >>> 0;
  const next = () => {
    x ^= x << 13;
    x ^= x >>> 17;
    x ^= x << 5;
    x >>>= 0;
    return x / 4294967296;
  };
  const st: Star[] = [];
  for (let i = 0; i < stars; i++) {
    const [px, py, s, a, w] = [next(), next(), next(), next(), next()];
    st.push({ x: px, y: py, size: 0.4 + s * s * 1.6, alpha: 0.15 + a * 0.85, warm: w });
  }
  const disk: Particle[] = [];
  for (let i = 0; i < particles; i++) {
    const [u, a, s, b, h] = [next(), next(), next(), next(), next()];
    const radius = 0.16 + 0.84 * u * u;
    disk.push({
      angle: a * TAU,
      radius,
      speed: 1.0 / (radius * Math.sqrt(radius)),
      size: 0.5 + s * 1.5,
      alpha: 0.2 + 0.8 * (1.0 - u) * (0.4 + 0.6 * b),
      hue: 18.0 + 30.0 * (1.0 - u) + h * 6.0,
    });
  }
  return { stars: st, disk };
}

/** The scene from the Rust engine (WebAssembly) when it is installed, else the TypeScript twin. */
export async function loadCosmos(seed = 1, stars = 220, particles = 1600): Promise<{ scene: Cosmos; engine: "wasm" | "js" }> {
  const w = await loadWasm();
  if (w?.cosmosScene) {
    try {
      const out = JSON.parse(w.cosmosScene(JSON.stringify({ seed, stars, particles })));
      if (!out.error) return { scene: out as Cosmos, engine: "wasm" };
    } catch {
      /* fall back */
    }
  }
  return { scene: cosmos(seed, stars, particles), engine: "js" };
}
