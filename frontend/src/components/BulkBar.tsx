import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useLayoutEffect, useRef, useState } from "react";
import { api, type Query } from "../api/client";
import { useWrite } from "../api/hooks";
import DeleteMediaButton from "./DeleteMedia";
import EntityPicker, { creator } from "./EntityPicker";
import { ErrorBox } from "./ui";

const CHUNK = 5000;
const MAX_CHIPS = 40;
interface Assigned { id: number; name: string; count: number }
type AssignedKind = "tags" | "performers" | "studios";

/** Fetch the ids of everything matching a list's filters that the user may edit. */
export async function fetchMatchingIds(kind: "scenes" | "images", query: Query): Promise<{ ids: number[]; truncated: boolean }> {
  return api(`/ids/${kind}`, { query });
}

/** Actions on a selection of scenes, galleries or images (one set-based request each). */
export default function BulkBar({ kind, ids, onDone, onSelectAll }: {
  kind: "scenes" | "galleries" | "images"; ids: number[]; onDone: () => void; onSelectAll: () => void;
}) {
  const [tags, setTags] = useState<number[]>([]);
  const [performers, setPerformers] = useState<number[]>([]);
  // Large selections ("select all matching") go to the server in chunks.
  const write = useWrite(async (body: object) => {
    for (let i = 0; i < ids.length; i += CHUNK) {
      await api(`/${kind}/bulk`, { json: { ids: ids.slice(i, i + CHUNK), ...body } });
    }
  });
  const [studio, setStudio] = useState<number[]>([]);
  const run = (body: object) => write.mutate(body, { onSuccess: () => { setTags([]); setPerformers([]); setStudio([]); } });

  // What the selection already has, so it can be seen and removed here (kept fresh by
  // useWrite, which refetches after every change).
  const assigned = useQuery({
    queryKey: ["assigned", kind, ids],
    queryFn: async () => {
      const merged: Record<AssignedKind, Map<number, Assigned>> = { tags: new Map(), performers: new Map(), studios: new Map() };
      for (let i = 0; i < ids.length; i += CHUNK) {
        const part = await api<Record<AssignedKind, Assigned[]>>(`/${kind}/assigned`, { json: { ids: ids.slice(i, i + CHUNK) } });
        for (const key of ["tags", "performers", "studios"] as const) {
          for (const a of part[key]) {
            const seen = merged[key].get(a.id);
            merged[key].set(a.id, seen ? { ...seen, count: seen.count + a.count } : a);
          }
        }
      }
      const sorted = (m: Map<number, Assigned>) => [...m.values()].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
      return { tags: sorted(merged.tags), performers: sorted(merged.performers), studios: sorted(merged.studios) };
    },
    placeholderData: keepPreviousData,
  });
  const chips = (key: AssignedKind, label: string) => {
    const list = assigned.data?.[key] ?? [];
    if (!list.length) return null;
    return (
      <div className="flex flex-wrap items-center gap-1.5" data-testid={`assigned-${key}`}>
        <span className="text-xs uppercase tracking-wide text-zinc-500">{label}</span>
        {list.slice(0, MAX_CHIPS).map((a) => (
          <span key={a.id} className={`chip ${key === "performers" ? "bg-accent/20 text-rose-200" : key === "studios" ? "bg-sky-900/50 text-sky-200" : ""}`}
                title={a.count === ids.length ? "On every selected item" : `On ${a.count} of the ${ids.length} selected items`}>
            {a.name}
            <span className="text-zinc-400">{a.count >= ids.length ? "all" : `${a.count}/${ids.length}`}</span>
            <button type="button" className="text-zinc-400 hover:text-white" disabled={write.isPending}
                    aria-label={`Remove ${a.name} from the selection`}
                    onClick={() => run(key === "studios" ? { clear_studio_ids: [a.id] } : { [key]: { mode: "remove", ids: [a.id] } })}>×</button>
          </span>
        ))}
        {list.length > MAX_CHIPS && <span className="text-xs text-zinc-500">and {list.length - MAX_CHIPS} more</span>}
      </div>
    );
  };

  // The bar is fixed over the bottom of the window. Reserve the same height at the end of
  // the page, so what is down there (the bottom page navigation) can be scrolled clear of it.
  const bar = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState(0);
  useLayoutEffect(() => {
    const el = bar.current;
    if (!el) return;
    const measure = () => setHeight(el.offsetHeight);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return (
    <>
    <div style={{ height: height + 16 }} aria-hidden data-testid="bulk-bar-spacer" />
    <div ref={bar} className="fixed inset-x-0 bottom-0 z-40 border-t border-zinc-700 bg-zinc-900/95 p-3 backdrop-blur" data-testid="bulk-bar" data-no-marquee>
      {(assigned.data?.tags.length || assigned.data?.performers.length || assigned.data?.studios.length) ? (
        <div className="mx-auto mb-2 flex max-h-24 max-w-[1920px] flex-wrap items-center gap-x-5 gap-y-1.5 overflow-y-auto" data-testid="assigned">
          <span className="text-xs text-zinc-400">Already assigned (× removes it from the selection):</span>
          {chips("performers", "Performers")}
          {chips("studios", "Studio")}
          {chips("tags", "Tags")}
        </div>
      ) : null}
      <div className="mx-auto flex max-w-[1920px] flex-wrap items-end gap-3">
        <div className="text-sm">
          <strong>{ids.length.toLocaleString()}</strong> selected
          <button className="ml-2 text-xs text-zinc-400 underline" onClick={onSelectAll}>select all shown</button>
          <button className="ml-2 text-xs text-zinc-400 underline" onClick={onDone}>clear</button>
        </div>
        <div className="w-64"><EntityPicker kind="tags" value={tags} onChange={setTags} onCreate={creator("tags")} placeholder="Tags…" /></div>
        <button className="btn-primary" disabled={!tags.length} onClick={() => run({ tags: { mode: "add", ids: tags } })}>Add tags</button>
        <button className="btn-ghost" disabled={!tags.length} onClick={() => run({ tags: { mode: "remove", ids: tags } })}>Remove tags</button>
        <div className="w-56"><EntityPicker kind="performers" value={performers} onChange={setPerformers} onCreate={creator("performers")} placeholder="Performers…" /></div>
        <button className="btn-primary" disabled={!performers.length} onClick={() => run({ performers: { mode: "add", ids: performers } })}>Add performers</button>
        <button className="btn-ghost" disabled={!performers.length} onClick={() => run({ performers: { mode: "remove", ids: performers } })}>Remove performers</button>
        <div className="w-52"><EntityPicker kind="studios" single value={studio} onChange={setStudio} onCreate={creator("studios")} placeholder="Studio…" /></div>
        <button className="btn-primary" disabled={!studio.length} onClick={() => run({ set: { studio_id: studio[0] } })}>Set studio</button>
        <button className="btn-ghost" disabled={!assigned.data?.studios.length}
                onClick={() => run({ clear_studio_ids: assigned.data!.studios.map((x) => x.id) })}>Clear studio</button>
        <select className="input w-auto" defaultValue="" onChange={(e) => e.target.value && run({ set: { rating: e.target.value === "none" ? null : +e.target.value } })} aria-label="Set rating">
          <option value="" disabled>Set rating…</option>
          {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n * 20}>{"★".repeat(n)}</option>)}
          <option value="none">No rating</option>
        </select>
        <button className="btn-ghost" onClick={() => run({ set: { organized: true } })}>Mark organized</button>
        <DeleteMediaButton kind={kind} ids={ids} onDeleted={onDone} />
        {write.isPending && <span className="text-sm text-zinc-400">Applying…</span>}
        {write.isSuccess && !write.isPending && <span className="text-sm text-emerald-400">Done</span>}
        {write.isError && <ErrorBox error={write.error} />}
      </div>
    </div>
    </>
  );
}
