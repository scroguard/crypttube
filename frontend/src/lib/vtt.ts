/** Parser for sprite WebVTT files: cues whose payload is `url#xywh=x,y,w,h`. */

export interface SpriteCue {
  start: number;
  end: number;
  url: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

function parseTime(t: string): number {
  const parts = t.trim().split(":").map(Number);
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}

/** `base` resolves relative sprite URLs (relative to the .vtt location). */
export function parseSpriteVtt(text: string, base: string): SpriteCue[] {
  const cues: SpriteCue[] = [];
  const lines = text.replace(/\r/g, "").split("\n");
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(/^([\d:.]+)\s+-->\s+([\d:.]+)/);
    if (!m) continue;
    const payload = (lines[i + 1] ?? "").trim();
    const frag = payload.match(/^(.*)#xywh=(\d+),(\d+),(\d+),(\d+)$/);
    if (!frag) continue;
    cues.push({
      start: parseTime(m[1]),
      end: parseTime(m[2]),
      url: new URL(frag[1], base).toString(),
      x: +frag[2],
      y: +frag[3],
      w: +frag[4],
      h: +frag[5],
    });
  }
  return cues;
}

export function cueAt(cues: SpriteCue[], time: number): SpriteCue | null {
  if (!cues.length) return null;
  let lo = 0;
  let hi = cues.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (cues[mid].start <= time) lo = mid;
    else hi = mid - 1;
  }
  return cues[lo];
}

const cache = new Map<string, Promise<SpriteCue[]>>();

/** Fetch + parse once per URL; resolves to [] when sprites aren't generated yet. */
export function loadSprites(vttUrl: string): Promise<SpriteCue[]> {
  let p = cache.get(vttUrl);
  if (!p) {
    p = fetch(vttUrl, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.text() : ""))
      .then((text) => parseSpriteVtt(text, new URL(vttUrl, location.origin).toString()))
      .catch(() => []);
    cache.set(vttUrl, p);
    p.then((cues) => cues.length || cache.delete(vttUrl)); // retry later if not ready
  }
  return p;
}
