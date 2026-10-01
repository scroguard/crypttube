/** Two-way mapping between URL search params and list-API query params. */

import type { Query } from "../api/client";

export type FieldKind = "string" | "number" | "bool" | "ids";

export type FilterSpec = Record<string, { param: string; kind: FieldKind }>;

export const SCENE_FILTERS: FilterSpec = {
  q: { param: "q", kind: "string" },
  sort: { param: "sort", kind: "string" },
  desc: { param: "desc", kind: "bool" },
  tags: { param: "tag_ids", kind: "ids" },
  tag_mode: { param: "tag_mode", kind: "string" },
  xtags: { param: "exclude_tag_ids", kind: "ids" },
  performers: { param: "performer_ids", kind: "ids" },
  studios: { param: "studio_ids", kind: "ids" },
  res: { param: "height_min", kind: "number" },
  rating: { param: "rating_min", kind: "number" },
  organized: { param: "organized", kind: "bool" },
  played: { param: "played", kind: "bool" },
  missing: { param: "file_status", kind: "string" },
  unassigned: { param: "no_performers", kind: "bool" },
};

export const GALLERY_FILTERS: FilterSpec = {
  q: SCENE_FILTERS.q,
  sort: SCENE_FILTERS.sort,
  desc: SCENE_FILTERS.desc,
  tags: SCENE_FILTERS.tags,
  tag_mode: SCENE_FILTERS.tag_mode,
  performers: SCENE_FILTERS.performers,
  studios: SCENE_FILTERS.studios,
  rating: SCENE_FILTERS.rating,
};

export const IMAGE_FILTERS: FilterSpec = {
  q: SCENE_FILTERS.q,
  sort: SCENE_FILTERS.sort,
  desc: SCENE_FILTERS.desc,
  tags: SCENE_FILTERS.tags,
  performers: SCENE_FILTERS.performers,
  unassigned: SCENE_FILTERS.unassigned,
};

export type FilterValues = Record<string, string | number | boolean | number[] | undefined>;

export function readFilters(params: URLSearchParams, spec: FilterSpec): FilterValues {
  const out: FilterValues = {};
  for (const [key, { kind }] of Object.entries(spec)) {
    if (kind === "ids") {
      const ids = params.getAll(key).map(Number).filter((n) => Number.isInteger(n) && n > 0);
      if (ids.length) out[key] = ids;
      continue;
    }
    const raw = params.get(key);
    if (raw === null || raw === "") continue;
    if (kind === "number") {
      const n = Number(raw);
      if (isFinite(n)) out[key] = n;
    } else if (kind === "bool") {
      if (raw === "1" || raw === "0") out[key] = raw === "1";
    } else out[key] = raw;
  }
  return out;
}

export function writeFilters(values: FilterValues, spec: FilterSpec): URLSearchParams {
  const params = new URLSearchParams();
  for (const [key, { kind }] of Object.entries(spec)) {
    const v = values[key];
    if (v === undefined || v === "" || (Array.isArray(v) && !v.length)) continue;
    if (kind === "ids") (v as number[]).forEach((id) => params.append(key, String(id)));
    else if (kind === "bool") params.set(key, v ? "1" : "0");
    else params.set(key, String(v));
  }
  return params;
}

export function toQuery(values: FilterValues, spec: FilterSpec): Query {
  const q: Query = {};
  for (const [key, { param }] of Object.entries(spec)) {
    const v = values[key];
    if (v !== undefined) q[param] = v as Query[string];
  }
  return q;
}
