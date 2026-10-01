import { useCallback, useState } from "react";
import { api, type Query } from "../api/client";
import { useInfiniteList, useMe, useWrite } from "../api/hooks";
import type { SceneItem } from "../api/types";
import { SCENE_FILTERS, toQuery } from "../lib/filters";
import { plural } from "../lib/format";
import { SceneCard } from "./Cards";
import EntityPicker, { creator } from "./EntityPicker";
import { SearchBox, SortControl, Toolbar, useUrlFilters, PageSizeSelect } from "./FilterBar";
import InfiniteGrid from "./InfiniteGrid";
import { ErrorBox, Field } from "./ui";

const SORTS = {
  created_at: "Date added",
  release_date: "Release date",
  title: "Title",
  duration: "Duration",
  resolution: "Resolution",
  rating: "Rating",
  play_count: "Most played",
  last_played_at: "Recently played",
  updated_at: "Recently edited",
};

const triState = (v: unknown) => (v === undefined ? "" : v ? "1" : "0");
const fromTri = (s: string) => (s === "" ? undefined : s === "1");

/** Scene grid with URL-synced filters. `fixed` pins filters (e.g. a performer's page). */
export default function SceneBrowser({ fixed = {} }: { fixed?: Query }) {
  const { canCreate, isAdmin } = useMe();
  const [f, update, reset] = useUrlFilters(SCENE_FILTERS, { sort: "created_at", desc: true });
  const [panel, setPanel] = useState(false);
  const [selected, setSelected] = useState<Set<number> | null>(null);
  const list = useInfiniteList<SceneItem>("/scenes", { ...toQuery(f, SCENE_FILTERS), ...fixed }, { withTotal: true });

  const active = ["tags", "xtags", "performers", "studios", "res", "rating", "organized", "played", "missing"]
    .filter((k) => f[k] !== undefined).length;
  const toggle = useCallback((id: number) => setSelected((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  }), []);

  return (
    <div>
      <Toolbar>
        <SearchBox value={(f.q as string) ?? ""} onChange={(q) => update({ q }, true)} placeholder="Search titles…" />
        <SortControl sorts={SORTS} sort={f.sort as string} desc={f.desc as boolean}
                     onChange={(sort, desc) => update({ sort, desc })} />
        <button className={panel || active ? "btn-primary" : "btn-ghost"} onClick={() => setPanel((p) => !p)} aria-expanded={panel}>
          Filters{active ? ` (${active})` : ""}
        </button>
        {active > 0 && <button className="btn-ghost" onClick={reset}>Clear</button>}
        {canCreate && (
          <button className={selected ? "btn-primary" : "btn-ghost"} onClick={() => setSelected((s) => (s ? null : new Set()))}>
            {selected ? "Done selecting" : "Select"}
          </button>
        )}
        <PageSizeSelect />
        <span className="ml-auto text-sm text-zinc-500" data-testid="total">
          {list.total != null && plural(list.total, "scene")}
        </span>
      </Toolbar>

      {panel && (
        <div className="card mb-4 grid gap-4 p-4 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Tags">
            <EntityPicker kind="tags" value={(f.tags as number[]) ?? []} onChange={(tags) => update({ tags })} />
            <select className="input mt-1 py-1 text-xs" value={(f.tag_mode as string) ?? "all"} onChange={(e) => update({ tag_mode: e.target.value })} aria-label="Tag mode">
              <option value="all">Must have all (incl. sub-tags)</option>
              <option value="any">Any of these</option>
            </select>
          </Field>
          <Field label="Exclude tags">
            <EntityPicker kind="tags" value={(f.xtags as number[]) ?? []} onChange={(xtags) => update({ xtags })} placeholder="Exclude…" />
          </Field>
          {!fixed.performer_ids && (
            <Field label="Performers">
              <EntityPicker kind="performers" value={(f.performers as number[]) ?? []} onChange={(performers) => update({ performers })} />
            </Field>
          )}
          {!fixed.studio_ids && (
            <Field label="Studios (incl. sub-studios)">
              <EntityPicker kind="studios" value={(f.studios as number[]) ?? []} onChange={(studios) => update({ studios })} />
            </Field>
          )}
          <Field label="Minimum resolution">
            <select className="input" value={(f.res as number) ?? ""} onChange={(e) => update({ res: e.target.value ? +e.target.value : undefined })}>
              <option value="">Any</option>
              {[480, 720, 1080, 1440, 2160].map((h) => <option key={h} value={h}>{h === 2160 ? "4K" : `${h}p`}</option>)}
            </select>
          </Field>
          <Field label="Minimum rating">
            <select className="input" value={(f.rating as number) ?? ""} onChange={(e) => update({ rating: e.target.value ? +e.target.value : undefined })}>
              <option value="">Any</option>
              {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n * 20}>{"★".repeat(n)}</option>)}
            </select>
          </Field>
          <Field label="Watched">
            <select className="input" value={triState(f.played)} onChange={(e) => update({ played: fromTri(e.target.value) })}>
              <option value="">Any</option><option value="1">Watched</option><option value="0">Unwatched</option>
            </select>
          </Field>
          <Field label="Organized">
            <select className="input" value={triState(f.organized)} onChange={(e) => update({ organized: fromTri(e.target.value) })}>
              <option value="">Any</option><option value="1">Organized</option><option value="0">Not organized</option>
            </select>
          </Field>
          {isAdmin && (
            <Field label="Files">
              <select className="input" value={(f.missing as string) ?? ""} onChange={(e) => update({ missing: e.target.value || undefined })}>
                <option value="">Present</option><option value="missing">Missing only</option><option value="all">All</option>
              </select>
            </Field>
          )}
        </div>
      )}

      <InfiniteGrid list={list} empty="No scenes match."
                    render={(s) => <SceneCard scene={s} selected={selected?.has(s.id)} onToggle={selected ? toggle : undefined} />} />
      {selected && selected.size > 0 && (
        <BulkBar ids={[...selected]} onDone={() => setSelected(new Set())} onSelectAll={() => setSelected(new Set(list.items.map((s) => s.id)))} />
      )}
    </div>
  );
}

function BulkBar({ ids, onDone, onSelectAll }: { ids: number[]; onDone: () => void; onSelectAll: () => void }) {
  const [tags, setTags] = useState<number[]>([]);
  const [performers, setPerformers] = useState<number[]>([]);
  const write = useWrite((body: object) => api("/scenes/bulk", { json: { ids, ...body } }));
  const run = (body: object) => write.mutate(body, { onSuccess: () => { setTags([]); setPerformers([]); } });

  return (
    <div className="fixed inset-x-0 bottom-0 z-40 border-t border-zinc-700 bg-zinc-900/95 p-3 backdrop-blur" data-testid="bulk-bar">
      <div className="mx-auto flex max-w-[1920px] flex-wrap items-end gap-3">
        <div className="text-sm">
          <strong>{ids.length}</strong> selected
          <button className="ml-2 text-xs text-zinc-400 underline" onClick={onSelectAll}>select all loaded</button>
          <button className="ml-2 text-xs text-zinc-400 underline" onClick={onDone}>clear</button>
        </div>
        <div className="w-64"><EntityPicker kind="tags" value={tags} onChange={setTags} onCreate={creator("tags")} placeholder="Tags…" /></div>
        <button className="btn-primary" disabled={!tags.length} onClick={() => run({ tags: { mode: "add", ids: tags } })}>Add tags</button>
        <button className="btn-ghost" disabled={!tags.length} onClick={() => run({ tags: { mode: "remove", ids: tags } })}>Remove tags</button>
        <div className="w-56"><EntityPicker kind="performers" value={performers} onChange={setPerformers} placeholder="Performers…" /></div>
        <button className="btn-primary" disabled={!performers.length} onClick={() => run({ performers: { mode: "add", ids: performers } })}>Add performers</button>
        <select className="input w-auto" defaultValue="" onChange={(e) => e.target.value && run({ set: { rating: e.target.value === "none" ? null : +e.target.value } })} aria-label="Set rating">
          <option value="" disabled>Set rating…</option>
          {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n * 20}>{"★".repeat(n)}</option>)}
          <option value="none">No rating</option>
        </select>
        <button className="btn-ghost" onClick={() => run({ set: { organized: true } })}>Mark organized</button>
        {write.isError && <ErrorBox error={write.error} />}
      </div>
    </div>
  );
}
