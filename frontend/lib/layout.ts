import type { GraphTable, Relation, SchemaGraph } from "./api";

export const CARD_W = 220;
export const HEAD_H = 34;
export const ROW_H = 20;
const GAP_X = 90;
const GAP_Y = 40;

export interface Placed {
  table: GraphTable;
  x: number;
  y: number;
  h: number;
}

export interface Edge {
  relation: Relation;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface Layout {
  tables: Placed[];
  edges: Edge[];
  width: number;
  height: number;
}

export const cardHeight = (t: GraphTable) => HEAD_H + Math.max(t.columns.length, 1) * ROW_H + 8;

/** Layered left-to-right layout: a table sits one column right of every table it references. */
export function layoutSchema(graph: SchemaGraph): Layout {
  const names = graph.tables.map((t) => t.name);
  const refs = new Map<string, Set<string>>(names.map((n) => [n, new Set()]));
  for (const r of graph.relations) {
    if (r.from.table !== r.to.table && refs.has(r.from.table) && refs.has(r.to.table)) refs.get(r.from.table)!.add(r.to.table);
  }

  const depth = new Map<string, number>();
  const visit = (n: string, trail: Set<string>): number => {
    if (depth.has(n)) return depth.get(n)!;
    if (trail.has(n)) return 0; // cycle: break it here
    trail.add(n);
    let d = 0;
    for (const m of refs.get(n)!) d = Math.max(d, visit(m, trail) + 1);
    trail.delete(n);
    depth.set(n, d);
    return d;
  };
  names.forEach((n) => visit(n, new Set()));

  const byDepth = new Map<number, GraphTable[]>();
  for (const t of graph.tables) {
    const d = depth.get(t.name) ?? 0;
    byDepth.set(d, [...(byDepth.get(d) ?? []), t]);
  }

  const placed = new Map<string, Placed>();
  let width = 0;
  let height = 0;
  for (const d of [...byDepth.keys()].sort((a, b) => a - b)) {
    let y = 20;
    const x = 20 + d * (CARD_W + GAP_X);
    for (const table of byDepth.get(d)!) {
      const h = cardHeight(table);
      placed.set(table.name, { table, x, y, h });
      y += h + GAP_Y;
      height = Math.max(height, y);
    }
    width = Math.max(width, x + CARD_W + 20);
  }

  const rowY = (p: Placed, column: string) => {
    const i = Math.max(
      p.table.columns.findIndex((c) => c.name === column),
      0,
    );
    return p.y + HEAD_H + i * ROW_H + ROW_H / 2;
  };

  const edges: Edge[] = [];
  for (const relation of graph.relations) {
    const a = placed.get(relation.from.table);
    const b = placed.get(relation.to.table);
    if (!a || !b) continue;
    // referencing table is to the right: leave its left edge, enter the referenced table's right edge
    const toRight = a.x > b.x;
    edges.push({
      relation,
      x1: toRight ? a.x : a.x + CARD_W,
      y1: rowY(a, relation.from.column),
      x2: toRight ? b.x + CARD_W : b.x,
      y2: rowY(b, relation.to.column),
    });
  }
  return { tables: [...placed.values()], edges, width: Math.max(width, 300), height: Math.max(height, 200) };
}
