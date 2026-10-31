export interface Matching { ids: number[]; truncated: boolean; limit: number; skipped: number }

/** Says what "Select all matching" just did, since most of it can't be seen from the grid. */
export function matchingNote(r: Matching, before: number, shown: number[]): string {
  const n = r.ids.length;
  const on = new Set(shown);
  const hidden = r.ids.filter((id) => !on.has(id)).length;
  const parts = [!n ? "Nothing here that you can edit."
    : r.truncated ? `Selected the first ${n.toLocaleString()} matches.`
    : `Selected all ${n.toLocaleString()} ${n === 1 ? "match" : "matches"}.`];
  if (hidden) parts.push(`${hidden.toLocaleString()} of them ${hidden === 1 ? "is" : "are"} not on this page.`);
  if (before) parts.push(`This replaced your earlier selection of ${before.toLocaleString()}.`);
  if (r.skipped) parts.push(`${r.skipped.toLocaleString()} you can't edit ${r.skipped === 1 ? "was" : "were"} skipped.`);
  return parts.join(" ");
}
