import { loadWasm } from "./stats";

export interface Star { x: number; y: number; size: number; alpha: number; warm: number }
export interface Particle { angle: number; radius: number; speed: number; size: number; alpha: number; hue: number }
export interface Cosmos { stars: Star[]; disk: Particle[] }

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
