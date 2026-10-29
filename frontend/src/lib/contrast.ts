/** WCAG contrast checks for the theme editor, following the brand's rules: text 4.5:1,
 *  outlines and graphics 3:1, on every background they are used on. */
import type { Colors, Role } from "./themes";

type Rgb = [number, number, number];

/** "#rrggbb" or "rgba(r, g, b, a)" → [r, g, b] (0–255) and alpha. */
export function parseColor(value: string): { rgb: Rgb; alpha: number } | null {
  let m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(value);
  if (m) return { rgb: [1, 2, 3].map((i) => parseInt(m![i], 16)) as Rgb, alpha: 1 };
  m = /^rgba\((\d{1,3}), ?(\d{1,3}), ?(\d{1,3}), ?(0|1|0?\.\d{1,3})\)$/.exec(value);
  if (m && [1, 2, 3].every((i) => Number(m![i]) <= 255)) {
    return { rgb: [Number(m[1]), Number(m[2]), Number(m[3])], alpha: Number(m[4]) };
  }
  return null;
}

function luminance([r, g, b]: Rgb): number {
  const lin = (c: number) => (c /= 255) <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

export function contrast(a: string, b: string): number {
  const [x, y] = [parseColor(a), parseColor(b)];
  if (!x || !y) return 0;
  const [hi, lo] = [luminance(x.rgb), luminance(y.rgb)].sort((p, q) => q - p);
  return (hi + 0.05) / (lo + 0.05);
}

const BACKGROUNDS: Role[] = ["bg", "surface", "surface-raised"];
/** [foreground, backgrounds, minimum] */
const RULES: [Role, Role[], number][] = [
  ["text", BACKGROUNDS, 4.5],
  ["text-muted", BACKGROUNDS, 4.5],
  ["text-subtle", BACKGROUNDS, 4.5],
  ["link", ["bg", "surface"], 4.5],   // link is never used on raised fills
  ["border-strong", [...BACKGROUNDS, "surface-hover"], 3],
  ["accent", BACKGROUNDS, 3],
  ["focus", BACKGROUNDS, 3],
  ["danger-solid-fg", ["danger-solid", "danger-solid-hover"], 4.5],
  ["accent-fg", ["accent"], 4.5],
  ["primary-fg", ["primary", "primary-hover"], 4.5],
  ["success", ["success-bg"], 4.5],
  ["warning", ["warning-bg"], 4.5],
  ["danger", ["danger-bg"], 4.5],
  ["info", ["info-bg"], 4.5],
  ["danger-solid-border", BACKGROUNDS, 3],
];

export interface Warning { fg: Role; bg: Role; ratio: number; min: number }

/** Every pairing below its minimum contrast. */
export function contrastWarnings(colors: Colors): Warning[] {
  const out: Warning[] = [];
  for (const [fg, bgs, min] of RULES) {
    for (const bg of bgs) {
      const ratio = contrast(colors[fg], colors[bg]);
      if (ratio < min) out.push({ fg, bg, ratio, min });
    }
  }
  return out;
}
