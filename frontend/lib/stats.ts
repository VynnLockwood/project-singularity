/** Column statistics on a sample of values. Same results as the Rust engine (engine/src/stats.rs):
 *  both are checked against engine/testdata/stats_cases.json. The browser uses WebAssembly when it is
 *  available and this TypeScript version otherwise. */

export interface ColumnStats {
  count: number;
  nulls: number;
  nullPct: number;
  distinct: number;
  top: { value: string; count: number }[];
  numeric: { min: number; max: number; mean: number; median: number; stddev: number } | null;
  text: { minLength: number; maxLength: number; avgLength: number } | null;
}

const TOP = 5;
const round = (x: number, places: number) => {
  const k = 10 ** places;
  return Math.round(x * k) / k;
};
const show = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v));
const codePoints = (s: string) => Array.from(s).length;

export function columnStats(values: unknown[]): ColumnStats {
  const count = values.length;
  const present = values.filter((v) => v !== null && v !== undefined);
  const nulls = count - present.length;

  const freq = new Map<string, number>();
  for (const v of present) freq.set(show(v), (freq.get(show(v)) ?? 0) + 1);
  const top = [...freq.entries()]
    .map(([value, c]) => ({ value, count: c }))
    .sort((a, b) => b.count - a.count || (a.value < b.value ? -1 : a.value > b.value ? 1 : 0))
    .slice(0, TOP);

  const numbers = present.flatMap((v) => {
    if (typeof v === "number") return Number.isFinite(v) ? [v] : [];
    if (typeof v === "string" && v.trim() !== "") {
      const n = Number(v.trim());
      return Number.isFinite(n) ? [n] : [];
    }
    return [];
  });

  let numeric: ColumnStats["numeric"] = null;
  if (present.length > 0 && numbers.length === present.length) {
    const sorted = [...numbers].sort((a, b) => a - b);
    const n = sorted.length;
    const mean = sorted.reduce((s, x) => s + x, 0) / n;
    const median = n % 2 === 1 ? sorted[(n - 1) / 2] : (sorted[n / 2 - 1] + sorted[n / 2]) / 2;
    const variance = sorted.reduce((s, x) => s + (x - mean) ** 2, 0) / n;
    numeric = {
      min: sorted[0], max: sorted[n - 1], mean: round(mean, 6), median: round(median, 6), stddev: round(Math.sqrt(variance), 6),
    };
  }

  let text: ColumnStats["text"] = null;
  if (!numeric && present.length > 0 && present.every((v) => typeof v === "string")) {
    const lens = (present as string[]).map(codePoints);
    text = {
      minLength: Math.min(...lens), maxLength: Math.max(...lens),
      avgLength: round(lens.reduce((s, x) => s + x, 0) / lens.length, 3),
    };
  }

  return { count, nulls, nullPct: count === 0 ? 0 : round((nulls * 100) / count, 3), distinct: freq.size, top, numeric, text };
}

export type Wasm = { columnStats: (json: string) => string; layout3d?: (json: string) => string };
let wasm: Wasm | null | undefined;

/** Try to load the WebAssembly build once (served at /engine/). Missing or failing is fine: we fall back to TypeScript. */
export async function loadWasm(): Promise<Wasm | null> {
  if (wasm !== undefined) return wasm;
  try {
    const url = "/engine/singularity_engine.js";
    const mod = await import(/* webpackIgnore: true */ url);
    await mod.default("/engine/singularity_engine_bg.wasm");
    wasm = { columnStats: mod.columnStats, layout3d: mod.layout3d };
  } catch {
    wasm = null;
  }
  return wasm;
}

/** Stats for one column, using WebAssembly when present. */
export async function computeStats(values: unknown[]): Promise<{ stats: ColumnStats; engine: "wasm" | "js" }> {
  const w = await loadWasm();
  if (w) {
    try {
      const out = JSON.parse(w.columnStats(JSON.stringify(values)));
      if (!out.error) return { stats: out as ColumnStats, engine: "wasm" };
    } catch {
      /* fall through */
    }
  }
  return { stats: columnStats(values), engine: "js" };
}
