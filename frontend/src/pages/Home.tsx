import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { api, type Query } from "../api/client";
import { useMe, useWrite } from "../api/hooks";
import type { GalleryItem, HomeConfig, HomeSection, ImageItem, Page, PerformerItem, SceneItem, StudioItem } from "../api/types";
import { GalleryCard, ImageTile, PerformerCard, SceneCard, StudioCard } from "../components/Cards";
import { LAYOUTS } from "../components/InfiniteGrid";
import Lightbox from "../components/Lightbox";
import { ErrorBox, Spinner } from "../components/ui";

type Item = SceneItem | GalleryItem | ImageItem | PerformerItem | StudioItem;

interface Source {
  path: string;
  query: Query;
  layout: keyof typeof LAYOUTS;
  seeAll?: string;
  render: (item: Item, all: Item[], index: number, openImage: (items: ImageItem[], i: number) => void) => ReactNode;
}

const scene: Source["render"] = (it) => <SceneCard scene={it as SceneItem} />;

/** Where each section type gets its items, and how they're drawn. */
const SOURCES: Record<string, Source> = {
  continue_watching: { path: "/me/continue-watching", query: {}, layout: "wide", render: scene },
  recent_scenes: { path: "/scenes", query: { sort: "created_at", desc: true }, layout: "wide", seeAll: "/scenes?sort=created_at&desc=1", render: scene },
  top_rated_scenes: { path: "/scenes", query: { sort: "rating", desc: true, rating_min: 1 }, layout: "wide", seeAll: "/scenes?sort=rating&desc=1", render: scene },
  my_uploads: { path: "/scenes", query: { mine: true, sort: "created_at", desc: true }, layout: "wide", seeAll: "/uploads", render: scene },
  recent_galleries: { path: "/galleries", query: { sort: "created_at", desc: true }, layout: "wide", seeAll: "/galleries",
                      render: (it) => <GalleryCard gallery={it as GalleryItem} /> },
  recent_images: { path: "/images", query: { sort: "created_at", desc: true }, layout: "square", seeAll: "/images",
                   render: (it, all, i, open) => <ImageTile image={it as ImageItem} onOpen={() => open(all as ImageItem[], i)} /> },
  favorite_performers: { path: "/performers", query: { favorite: true }, layout: "portrait", seeAll: "/performers?favorite=1",
                         render: (it) => <PerformerCard performer={it as PerformerItem} /> },
  recent_performers: { path: "/performers", query: { sort: "created_at", desc: true }, layout: "portrait", seeAll: "/performers?sort=created_at&desc=1",
                       render: (it) => <PerformerCard performer={it as PerformerItem} /> },
  studios: { path: "/studios", query: {}, layout: "portrait", seeAll: "/studios", render: (it) => <StudioCard studio={it as StudioItem} /> },
};

const LIMITS = [6, 12, 18, 24, 36, 48];

function useHome() {
  return useQuery({ queryKey: ["home"], queryFn: () => api<HomeConfig>("/me/home") });
}

export default function Home() {
  const home = useHome();
  const { isAdmin } = useMe();
  const [editing, setEditing] = useState(false);
  const [box, setBox] = useState<{ items: ImageItem[]; index: number } | null>(null);
  // Sections report whether they have anything, so an all-empty home can say so.
  const [filled, setFilled] = useState<Record<string, boolean>>({});

  if (home.isPending) return <Spinner className="mt-24" />;
  if (home.isError) return <ErrorBox error={home.error} />;
  const labels = Object.fromEntries(home.data.available.map((a) => [a.type, a.label]));
  const sections = home.data.sections;
  const keyOf = (s: HomeSection, i: number) => `${i}-${s.type}-${s.limit}`;
  const nothing = sections.length > 0 && sections.every((s, i) => filled[keyOf(s, i)] === false);

  return (
    <div>
      <div className="mb-4 flex items-center">
        <h1 className="mr-auto text-2xl font-semibold">Home</h1>
        {!editing && <button className="btn-ghost" onClick={() => setEditing(true)}>Customize</button>}
      </div>
      {editing && <HomeEditor config={home.data} onClose={() => setEditing(false)} />}
      {!editing && sections.length === 0 && (
        <p className="py-16 text-center text-sm text-zinc-500" data-testid="home-empty">
          Your home page has no sections. Use Customize to add some.
        </p>
      )}
      {!editing && nothing && (
        <p className="py-16 text-center text-sm text-zinc-500" data-testid="home-empty">
          Nothing to show yet.{" "}
          {isAdmin ? <>Add a library under <Link to="/admin/libraries" className="text-accent-hover hover:underline">Admin → Libraries</Link>.</>
                   : "Content will appear here once it has been added."}
        </p>
      )}
      {!editing && sections.map((s, i) => (
        <SectionView key={keyOf(s, i)} section={s} label={s.title || labels[s.type] || s.type}
                     onFilled={(has) => setFilled((f) => (f[keyOf(s, i)] === has ? f : { ...f, [keyOf(s, i)]: has }))}
                     openImage={(items, index) => setBox({ items, index })} />
      ))}
      {box && <Lightbox images={box.items} index={box.index} onIndex={(index) => setBox({ ...box, index })} onClose={() => setBox(null)} />}
    </div>
  );
}

function SectionView({ section, label, onFilled, openImage }: {
  section: HomeSection; label: string; onFilled: (has: boolean) => void;
  openImage: (items: ImageItem[], index: number) => void;
}) {
  const src = SOURCES[section.type];
  const q = useQuery({
    queryKey: ["home-section", section.type, section.limit],
    queryFn: () => api<Page<Item>>(src.path, { query: { ...src.query, limit: section.limit } }),
    enabled: !!src,
  });
  const items = q.data?.items ?? [];
  const has = q.isSuccess ? items.length > 0 : null;
  useEffect(() => {
    if (has !== null) onFilled(has);
  }, [has]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!src || !items.length) return null;   // empty sections stay out of the way
  return (
    <section className="mb-8" aria-label={label} data-testid="home-section">
      <header className="mb-3 flex items-baseline gap-3 border-b border-zinc-800 pb-2">
        <h2 className="text-lg font-semibold">{label}</h2>
        {src.seeAll && <Link to={src.seeAll} className="ml-auto text-sm text-accent-hover hover:underline">See all →</Link>}
      </header>
      <div className={LAYOUTS[src.layout]}>
        {items.map((it, i) => <div key={it.id}>{src.render(it, items, i, openImage)}</div>)}
      </div>
    </section>
  );
}

/** Choose, order and size the sections. Nothing is saved until "Save". */
function HomeEditor({ config, onClose }: { config: HomeConfig; onClose: () => void }) {
  const [sections, setSections] = useState<HomeSection[]>(config.sections);
  const [adding, setAdding] = useState("");
  const labels = Object.fromEntries(config.available.map((a) => [a.type, a]));
  const save = useWrite((body: HomeSection[]) => api("/me/home", { method: "PUT", json: { sections: body } }));
  const reset = useWrite(() => api("/me/home", { method: "DELETE" }));

  const move = (i: number, by: number) => setSections((s) => {
    const next = [...s];
    const j = i + by;
    if (j < 0 || j >= next.length) return s;
    [next[i], next[j]] = [next[j], next[i]];
    return next;
  });
  const patch = (i: number, change: Partial<HomeSection>) =>
    setSections((s) => s.map((x, k) => (k === i ? { ...x, ...change } : x)));

  return (
    <div className="card mb-8 p-4" data-testid="home-editor">
      <h2 className="mb-1 font-semibold">Customize your home page</h2>
      <p className="mb-4 text-sm text-zinc-400">Pick what you want to see and in which order. This only affects your account.</p>
      <ol className="space-y-2">
        {sections.map((s, i) => (
          <li key={i} className="flex flex-wrap items-center gap-2 rounded-md bg-zinc-950 p-2" data-testid="home-row">
            <div className="flex flex-col">
              <button className="px-2 text-xs text-zinc-400 hover:text-white disabled:opacity-30" disabled={i === 0}
                      onClick={() => move(i, -1)} aria-label={`Move ${labels[s.type]?.label ?? s.type} up`}>▲</button>
              <button className="px-2 text-xs text-zinc-400 hover:text-white disabled:opacity-30" disabled={i === sections.length - 1}
                      onClick={() => move(i, 1)} aria-label={`Move ${labels[s.type]?.label ?? s.type} down`}>▼</button>
            </div>
            <div className="min-w-0 flex-1">
              <div className="font-medium">{labels[s.type]?.label ?? s.type}</div>
              <div className="truncate text-xs text-zinc-500">{labels[s.type]?.description}</div>
            </div>
            <input className="input w-48" placeholder="Custom heading (optional)" maxLength={60} value={s.title ?? ""}
                   onChange={(e) => patch(i, { title: e.target.value || null })} aria-label="Custom heading" />
            <select className="input w-auto" value={s.limit} onChange={(e) => patch(i, { limit: +e.target.value })} aria-label="Number of items">
              {LIMITS.map((n) => <option key={n} value={n}>{n} items</option>)}
            </select>
            <button className="btn-ghost py-1" onClick={() => setSections((x) => x.filter((_, k) => k !== i))}
                    aria-label={`Remove ${labels[s.type]?.label ?? s.type}`}>Remove</button>
          </li>
        ))}
        {!sections.length && <li className="py-4 text-center text-sm text-zinc-500">No sections. Add one below.</li>}
      </ol>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <select className="input w-auto" value={adding} onChange={(e) => setAdding(e.target.value)} aria-label="Section to add">
          <option value="">Add a section…</option>
          {config.available.map((a) => <option key={a.type} value={a.type}>{a.label}</option>)}
        </select>
        <button className="btn-ghost" disabled={!adding || sections.length >= 20}
                onClick={() => { setSections((s) => [...s, { type: adding, limit: 12, title: null }]); setAdding(""); }}>
          Add
        </button>
        <span className="flex-1" />
        <button className="btn-ghost" disabled={reset.isPending}
                onClick={() => reset.mutate(undefined, { onSuccess: onClose })}>Reset to default</button>
        <button className="btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn-primary" disabled={save.isPending} onClick={() => save.mutate(sections, { onSuccess: onClose })}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
      </div>
      {(save.error || reset.error) ? <ErrorBox error={save.error || reset.error} className="mt-3" /> : null}
    </div>
  );
}
