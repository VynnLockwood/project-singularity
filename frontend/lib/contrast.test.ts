import { readFileSync } from "node:fs";
import path from "node:path";
import { contrastRatio, readTokens } from "./contrast";

const t = readTokens(readFileSync(path.resolve(__dirname, "../app/tokens.css"), "utf8"));
const TEXT_PAIRS: [string, string][] = [
  ["--text", "--void"], ["--text", "--surface-1"], ["--text", "--surface-2"], ["--text", "--surface-3"],
  ["--text-bright", "--surface-1"], ["--text-dim", "--void"], ["--text-dim", "--surface-2"],
  ["--text-faint", "--void"], ["--text-faint", "--surface-1"],
  ["--on-disk", "--disk"], ["--blueshift", "--blueshift-tint"], ["--redshift", "--redshift-tint"],
  ["--disk-hot", "--disk-tint"], ["--blueshift", "--surface-1"], ["--redshift", "--surface-1"],
];

it("black on white is 21:1", () => expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 1));
it.each(TEXT_PAIRS)("%s on %s meets WCAG AA for text", (fg, bg) =>
  expect(contrastRatio(t[fg], t[bg])).toBeGreaterThanOrEqual(4.5));
it("UI marks meet 3:1", () => {
  expect(contrastRatio(t["--disk"], t["--void"])).toBeGreaterThanOrEqual(3);
  expect(contrastRatio(t["--line-strong"], t["--void"])).toBeGreaterThanOrEqual(1.5); // hairline, decorative
});
it("defines every colour token in the spec", () => {
  for (const k of ["--void","--surface-1","--surface-2","--surface-3","--line","--line-strong","--text","--text-bright","--text-dim","--text-faint","--disk","--disk-hot","--blueshift","--redshift","--blueshift-tint","--redshift-tint","--disk-tint","--on-disk"])
    expect(t[k]).toMatch(/^#[0-9a-f]{6}$/i);
});
