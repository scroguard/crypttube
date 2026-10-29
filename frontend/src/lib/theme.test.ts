import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { contrast, contrastWarnings, parseColor } from "./contrast";
import { PRESETS, ROLES, ROLE_GROUPS } from "./themes";

describe("theme", () => {
  it("measures contrast like WCAG", () => {
    expect(contrast("#FFFFFF", "#000000")).toBeCloseTo(21);
    expect(contrast("#9E77D0", "#262B3B")).toBeCloseTo(4.03, 2);   // link on raised (brand v0.3)
    expect(parseColor("rgba(5, 5, 8, 0.72)")).toEqual({ rgb: [5, 5, 8], alpha: 0.72 });
    expect(parseColor("#9164ca")).toEqual({ rgb: [145, 100, 202], alpha: 1 });
    for (const bad of ["red", "#9164C", "#9164CAFF", "rgba(256, 0, 0, 0.5)", "rgba(0, 0, 0, 2)", "rgb(0, 0, 0)"]) {
      expect(parseColor(bad), bad).toBeNull();
    }
  });

  it("every preset sets every role to a valid color and passes the brand's contrast rules", () => {
    for (const p of PRESETS) {
      expect(Object.keys(p.colors).sort()).toEqual([...ROLES].sort());
      for (const r of ROLES) expect(parseColor(p.colors[r]), `${p.id} ${r}`).not.toBeNull();
      expect(contrastWarnings(p.colors), p.id).toEqual([]);
    }
  });

  it("the editor lists every role once", () => {
    expect(ROLE_GROUPS.flatMap((g) => g.roles.map(([r]) => r)).sort()).toEqual([...ROLES].sort());
  });

  it("the built-in colors in index.css are the Crypt preset", () => {
    const css = readFileSync("src/index.css", "utf8");
    const crypt = PRESETS.find((p) => p.id === "crypt")!.colors;
    for (const r of ROLES) {
      const m = new RegExp(`--color-${r}: ([^;]+);`).exec(css);
      expect(m?.[1].replace(/\s/g, "").toLowerCase(), r).toBe(crypt[r].replace(/\s/g, "").toLowerCase());
    }
  });
});
