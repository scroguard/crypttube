import { useCallback, useState } from "react";
import type { Query } from "../api/client";
import { useInfiniteList, useMe } from "../api/hooks";
import type { SceneItem } from "../api/types";
import { SCENE_FILTERS, toQuery } from "../lib/filters";
import { plural } from "../lib/format";
import { SceneCard } from "./Cards";
import EntityPicker from "./EntityPicker";
import { SearchBox, SortControl, Toolbar, useUrlFilters, PageSizeSelect } from "./FilterBar";
import GroupedBrowser, { ViewToggle } from "./GroupedBrowser";
import InfiniteGrid, { LAYOUTS } from "./InfiniteGrid";
import { releaseFocus, useEscape } from "../lib/useEscape";
import { useBrowseView } from "../lib/viewPref";
import BulkBar, { fetchMatchingIds } from "./BulkBar";
import { Field } from "./ui";

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
export default function SceneBrowser({ fixed = {}, groupable = false }: { fixed?: Query; groupable?: boolean }) {
  const { canCreate, isAdmin } = useMe();
  const [f, update, reset] = useUrlFilters(SCENE_FILTERS, { sort: "created_at", desc: true });
  const [panel, setPanel] = useState(false);
  const [selected, setSelected] = useState<Set<number> | null>(null);
  const view = useBrowseView();
  // Grouping by performer makes no sense once a performer (or "unassigned") is selected,
  // and selecting for bulk edits needs the flat grid.
  const grouped = groupable && view === "grouped" && !f.performers && f.unassigned === undefined && !selected;
  const query = { ...toQuery(f, SCENE_FILTERS), ...fixed };
  const list = useInfiniteList<SceneItem>("/scenes", query, { withTotal: true, enabled: !grouped });

  const [note, setNote] = useState<string | null>(null);
  useEscape(!!selected, () => { setSelected(null); setNote(null); });   // Esc leaves select mode
  // Adds to what is already selected, so pages can be gone through one by one.
  const selectShown = () => {
    setSelected((s) => new Set([...(s ?? []), ...list.items.filter((i) => (i as { can_edit?: boolean }).can_edit !== false).map((i) => i.id)]));
    setNote(null);
  };
  async function selectAllMatching() {
    const r = await fetchMatchingIds("scenes", query);
    setSelected(new Set(r.ids));
    setNote(r.truncated ? "Selected the first 20,000 matches." : r.ids.length ? null : "Nothing here that you can edit.");
  }
  const active = ["tags", "xtags", "performers", "unassigned", "studios", "res", "rating", "organized", "played", "missing"]
    .filter((k) => f[k] !== undefined).length;
  const toggle = useCallback((id: number) => setSelected((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  }), []);

  return (
    <div>
      <Toolbar>
        <SearchBox value={(f.q as string) ?? ""} onChange={(q) => update({ q }, true)} placeholder="Search titles or folders…" />
        <SortControl sorts={SORTS} sort={f.sort as string} desc={f.desc as boolean}
                     onChange={(sort, desc) => update({ sort, desc })} />
        {groupable && <ViewToggle />}
        <button className={panel || active ? "btn-primary" : "btn-ghost"} onClick={() => setPanel((p) => !p)} aria-expanded={panel}>
          Filters{active ? ` (${active})` : ""}
        </button>
        {active > 0 && <button className="btn-ghost" onClick={reset}>Clear</button>}
        {canCreate && (
          <button className={selected ? "btn-primary" : "btn-ghost"} onClick={() => { releaseFocus(); setSelected((s) => (s ? null : new Set())); setNote(null); }}>
            {selected ? "Done selecting" : "Select"}
          </button>
        )}
        {selected && (
          <>
          <button className="btn-ghost" onClick={selectShown}
                    title={`Add every scene ${list.paging ? "on this page" : "shown so far"} to the selection`}>
              {list.paging ? "Select this page" : "Select all shown"}
            </button>
            <button className="btn-ghost" onClick={selectAllMatching} title="Select every scene that matches the current search and filters">
            Select all matching
          </button>
          </>
        )}
        {note && <span className="text-xs text-amber-400">{note}</span>}
        {!grouped && <PageSizeSelect />}
        <span className="ml-auto text-sm text-zinc-500" data-testid="total">
          {!grouped && list.total != null && plural(list.total, "scene")}
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
              <label className="mt-1 flex items-center gap-2 text-xs text-zinc-400">
                <input type="checkbox" className="accent-accent" checked={f.unassigned === true}
                       onChange={(e) => update({ unassigned: e.target.checked ? true : undefined })} />
                Only scenes with no performer
              </label>
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

      {grouped ? (
        <GroupedBrowser<SceneItem> kind="scenes" query={query} layout={LAYOUTS.wide} noun="scene" empty="No scenes match."
                                   renderItem={(s) => <SceneCard scene={s} />} />
      ) : (
        <InfiniteGrid list={list} empty="No scenes match."
                      render={(s) => <SceneCard scene={s} selected={selected?.has(s.id)} onToggle={selected ? toggle : undefined} />} />
      )}
      {/* The same selection buttons below a page, so they're at hand after scrolling down. */}
      {canCreate && !grouped && list.paging && list.items.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center justify-center gap-2" data-testid="select-bottom">
          <button className={selected ? "btn-primary" : "btn-ghost"} onClick={() => { releaseFocus(); setSelected((s) => (s ? null : new Set())); setNote(null); }}>
            {selected ? "Done selecting" : "Select"}
          </button>
          {selected && (
            <>
              <button className="btn-ghost" onClick={selectShown}>Select this page</button>
              <button className="btn-ghost" onClick={selectAllMatching}>Select all matching</button>
            </>
          )}
        </div>
      )}
      {selected && selected.size > 0 && (
        <BulkBar kind="scenes" ids={[...selected]} onDone={() => setSelected(new Set())} onSelectAll={selectShown} />
      )}
    </div>
  );
}
