import type { IconName } from "@/components/ui";

export interface Command {
  id: string;
  label: string;
  hint?: string;
  group: "Go to" | "Tables" | "Actions";
  icon: IconName;
  run: (e: { shift: boolean }) => void;
}

const GROUPS: Command["group"][] = ["Go to", "Tables", "Actions"];

/**
 * Fuzzy match: the query's letters must appear in the label in order. Returns a score (lower is
 * better: where the match starts plus how far it is spread out), or null when there is no match.
 */
export function match(query: string, label: string): number | null {
  const q = query.toLowerCase().replace(/\s+/g, "");
  if (q === "") return 0;
  const text = label.toLowerCase();
  let best: number | null = null;
  for (let start = text.indexOf(q[0]); start !== -1; start = text.indexOf(q[0], start + 1)) {
    let at = start;
    let ok = true;
    for (let i = 1; i < q.length; i++) {
      at = text.indexOf(q[i], at + 1);
      if (at === -1) { ok = false; break; }
    }
    if (!ok) break; // a later start can only have less text left to match in
    const score = start + (at - start + 1);
    if (best === null || score < best) best = score;
  }
  return best;
}

/** The commands that match, best first. Ties keep the group order, then the order given. */
export function rank(query: string, commands: Command[], limit = 12): Command[] {
  return commands
    .map((c, i) => ({ c, i, score: match(query, c.label) }))
    .filter((x): x is { c: Command; i: number; score: number } => x.score !== null)
    .sort((a, b) => a.score - b.score || GROUPS.indexOf(a.c.group) - GROUPS.indexOf(b.c.group) || a.i - b.i)
    .slice(0, limit)
    .map((x) => x.c);
}
