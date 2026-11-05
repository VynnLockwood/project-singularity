import type { SchemaGraph } from "./api";
import { loadWasm } from "./stats";

export type Vec3 = [number, number, number];

const fract = (x: number) => x - Math.floor(x);
const initial = (i: number): Vec3 => {
  const k = i + 1;
  return [fract(k * 0.8191725133961645) - 0.5, fract(k * 0.6710436067037893) - 0.5, fract(k * 0.5497004779019703) - 0.5];
};

/** TypeScript twin of engine/src/layout.rs (same arithmetic, same golden cases). */
export function layout3d(nodes: number, edges: [number, number][], iterations = 200, radius = 100): Vec3[] {
  if (nodes <= 1) return Array.from({ length: nodes }, () => [0, 0, 0] as Vec3);
  const p: Vec3[] = Array.from({ length: nodes }, (_, i) => initial(i));
  const valid = edges.filter(([a, b]) => a !== b && a >= 0 && b >= 0 && a < nodes && b < nodes);
  let temp = 0.5;
  for (let it = 0; it < iterations; it++) {
    const disp: Vec3[] = Array.from({ length: nodes }, () => [0, 0, 0]);
    for (let i = 0; i < nodes; i++) {
      for (let j = i + 1; j < nodes; j++) {
        const d: Vec3 = [p[i][0] - p[j][0], p[i][1] - p[j][1], p[i][2] - p[j][2]];
        const dist = Math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2] + 0.0001);
        const f = 1.0 / (dist * dist);
        for (let a = 0; a < 3; a++) {
          const v = (d[a] / dist) * f;
          disp[i][a] += v;
          disp[j][a] -= v;
        }
      }
    }
    for (const [a, b] of valid) {
      const d: Vec3 = [p[a][0] - p[b][0], p[a][1] - p[b][1], p[a][2] - p[b][2]];
      const dist = Math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2] + 0.000001);
      for (let k = 0; k < 3; k++) {
        const v = (d[k] / dist) * dist * dist;
        disp[a][k] -= v;
        disp[b][k] += v;
      }
    }
    for (let i = 0; i < nodes; i++) {
      for (let k = 0; k < 3; k++) disp[i][k] -= p[i][k] * 0.05;
      const len = Math.sqrt(disp[i][0] * disp[i][0] + disp[i][1] * disp[i][1] + disp[i][2] * disp[i][2]);
      if (len > 0) {
        const step = len < temp ? len : temp;
        for (let k = 0; k < 3; k++) p[i][k] += (disp[i][k] / len) * step;
      }
    }
    temp *= 0.97;
  }
  const c: Vec3 = [0, 0, 0];
  for (const q of p) for (let k = 0; k < 3; k++) c[k] += q[k] / nodes;
  let far = 0;
  for (const q of p) {
    for (let k = 0; k < 3; k++) q[k] -= c[k];
    far = Math.max(far, Math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2]));
  }
  if (far > 0) {
    const s = radius / far;
    for (const q of p) for (let k = 0; k < 3; k++) q[k] *= s;
  }
  return p;
}

export interface Body {
  name: string;
  pos: Vec3;
  /** sphere radius: grows slowly with row count */
  size: number;
  rows: number;
  columns: number;
}
export interface Link {
  from: string;
  to: string;
  fromColumn: string;
}
export interface Universe {
  bodies: Body[];
  links: Link[];
  engine: "wasm" | "js";
}

export const bodySize = (rows: number) => 3 + Math.log10(Math.max(rows, 1) + 1) * 2;

export async function layoutUniverse(graph: SchemaGraph): Promise<Universe> {
  const names = graph.tables.map((t) => t.name);
  const index = new Map(names.map((n, i) => [n, i]));
  const rel = graph.relations.filter((r) => index.has(r.from.table) && index.has(r.to.table) && r.from.table !== r.to.table);
  const edges = rel.map((r) => [index.get(r.from.table)!, index.get(r.to.table)!] as [number, number]);

  let positions: Vec3[] | null = null;
  let engine: "wasm" | "js" = "js";
  const w = await loadWasm();
  if (w?.layout3d) {
    try {
      const out = JSON.parse(w.layout3d(JSON.stringify({ nodes: names.length, edges })));
      if (!out.error) { positions = out.positions as Vec3[]; engine = "wasm"; }
    } catch {
      /* fall back */
    }
  }
  if (!positions) positions = layout3d(names.length, edges);

  return {
    engine,
    bodies: graph.tables.map((t, i) => ({
      name: t.name, pos: positions![i], size: bodySize(t.rowEstimate), rows: t.rowEstimate, columns: t.columns.length,
    })),
    links: rel.map((r) => ({ from: r.from.table, to: r.to.table, fromColumn: r.from.column })),
  };
}
