import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { api, buildUrl } from "../api/client";
import { useMe, useWrite } from "../api/hooks";
import type { ImageItem, Page, SceneItem } from "../api/types";
import type { BrowseResult } from "./DirectoryPicker";
import { ErrorBox, Field, Modal, Spinner, Tabs } from "./ui";

type Kind = "performers" | "studios" | "tags";
type Source = "upload" | "url" | "library" | "server";

/**
 * Choose a picture for a performer / studio / tag: upload one, fetch it from a URL, pick
 * something already in the library, or (admins) pick any image file from the media store.
 */
export default function ImageSourceModal({ kind, id, onClose }: { kind: Kind; id: number; onClose: () => void }) {
  const { isAdmin } = useMe();
  const [tab, setTab] = useState<Source>("upload");
  const base = `/${kind}/${id}/image`;
  // Every source ends the same way: the server stores the picture, then we close.
  const apply = useWrite((run: () => Promise<unknown>) => run());
  const done = { onSuccess: onClose };
  const tabs: Record<string, string> = { upload: "Upload", url: "From URL", library: "From library" };
  if (isAdmin) tabs.server = "Server files";

  return (
    <Modal title="Choose an image" onClose={onClose} wide>
      <RandomSource kind={kind} base={base} onClose={onClose} />
      <Tabs tabs={tabs as Record<Source, string>} value={tab} onChange={setTab} />
      {tab === "upload" && (
        <div className="py-6 text-center">
          <label className="btn-primary cursor-pointer">
            Choose a file…
            <input type="file" accept="image/*" className="hidden" data-testid="image-upload"
                   onChange={(e) => {
                     const file = e.target.files?.[0];
                     if (file) apply.mutate(() => api(base, { method: "PUT", body: file, headers: { "Content-Type": file.type || "application/octet-stream" } }), done);
                   }} />
          </label>
          <p className="mt-3 text-xs text-zinc-500">Any common image format, up to 20 MB. It is stored as an optimized copy.</p>
        </div>
      )}
      {tab === "url" && <UrlSource onFetch={(url) => apply.mutate(() => api(`${base}/from-url`, { json: { url } }), done)} busy={apply.isPending} />}
      {tab === "library" && (
        <LibrarySource kind={kind} id={id}
                       onPick={(body) => apply.mutate(() => api(`${base}/from-library`, { json: body }), done)} />
      )}
      {tab === "server" && isAdmin && (
        <ServerSource onPick={(path) => apply.mutate(() => api(`${base}/from-path`, { json: { path } }), done)} />
      )}
      {apply.isPending && tab !== "url" && <p className="mt-3 text-sm text-zinc-400">Saving…</p>}
      {apply.isError && <ErrorBox error={apply.error} className="mt-3" />}
    </Modal>
  );
}

/**
 * "Surprise me": use a random one of the images assigned to this performer (or studio, or
 * tag). The dialog stays open so the button can be pressed again for a different one.
 */
function RandomSource({ kind, base, onClose }: { kind: Kind; base: string; onClose: () => void }) {
  const pick = useWrite((exclude: number | null) =>
    api<{ image_id: number; thumbnail_url: string }>(`${base}/random`, { json: { exclude_image_id: exclude } }));
  const self = kind === "performers" ? "this performer's" : kind === "studios" ? "this studio's" : "this tag's";
  return (
    <div className="mb-4 flex flex-wrap items-center gap-3 rounded-md border border-zinc-800 bg-zinc-950/40 p-3" data-testid="random-source">
      {pick.data && (
        <img src={pick.data.thumbnail_url} alt="Current pick" data-testid="random-pick" data-image-id={pick.data.image_id}
             className={`h-16 w-16 rounded-md object-cover ${pick.isPending ? "opacity-50" : ""}`} />
      )}
      <button type="button" className="btn-primary" disabled={pick.isPending}
              onClick={() => pick.mutate(pick.data?.image_id ?? null)}>
        🎲 {pick.data ? "Pick another" : "Random"}
      </button>
      <span className="min-w-0 flex-1 text-sm text-zinc-400">
        {pick.isError ? <span className="text-red-300" role="alert">{pick.error.message}</span>
          : pick.data ? "Saved. Not the one? Pick another."
          : `Use a random one of ${self} images.`}
      </span>
      {pick.data && <button type="button" className="btn-ghost" onClick={onClose}>Done</button>}
    </div>
  );
}

function UrlSource({ onFetch, busy }: { onFetch: (url: string) => void; busy: boolean }) {
  const [url, setUrl] = useState("");
  function submit(e: FormEvent) {
    e.preventDefault();
    onFetch(url.trim());
  }
  return (
    <form onSubmit={submit} className="space-y-3 py-2">
      <Field label="Image address">
        <input className="input" type="url" placeholder="https://…" value={url} onChange={(e) => setUrl(e.target.value)} required />
      </Field>
      <p className="text-xs text-zinc-500">
        The server downloads the picture once and keeps its own copy; nothing is loaded from that site afterwards.
        Only addresses on the public internet are allowed.
      </p>
      <div className="flex justify-end">
        <button className="btn-primary" disabled={busy || !url.trim()}>{busy ? "Fetching…" : "Fetch image"}</button>
      </div>
    </form>
  );
}

const GRID = "grid max-h-[50vh] grid-cols-[repeat(auto-fill,minmax(110px,1fr))] gap-2 overflow-y-auto";

function LibrarySource({ kind, id, onPick }: { kind: Kind; id: number; onPick: (body: { image_id?: number; scene_id?: number }) => void }) {
  const [what, setWhat] = useState<"images" | "scenes">("images");
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [only, setOnly] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setQ(text.trim()), 250);
    return () => clearTimeout(t);
  }, [text]);

  const filter = only ? { [kind === "performers" ? "performer_ids" : kind === "studios" ? "studio_ids" : "tag_ids"]: [id] } : {};
  const list = useInfiniteQuery({
    queryKey: ["image-source", what, q, filter],
    queryFn: ({ pageParam }) =>
      api<Page<ImageItem | SceneItem>>(`/${what}`, { query: { ...filter, q: q || undefined, limit: 60, thumb_w: 320, cursor: pageParam } }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = list.data?.pages.flatMap((p) => p.items) ?? [];
  const self = kind === "performers" ? "this performer" : kind === "studios" ? "this studio" : "this tag";

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <select className="input w-auto" value={what} onChange={(e) => setWhat(e.target.value as "images" | "scenes")} aria-label="Pick from">
          <option value="images">Images</option>
          <option value="scenes">Video thumbnails</option>
        </select>
        <input className="input max-w-xs" type="search" placeholder="Search titles…" value={text} onChange={(e) => setText(e.target.value)} aria-label="Search library" />
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" className="accent-accent" checked={only} onChange={(e) => setOnly(e.target.checked)} /> Only {self}
        </label>
      </div>
      {list.isPending ? <Spinner className="my-10" /> : list.isError ? <ErrorBox error={list.error} /> : (
        <div className={GRID} data-testid="library-source">
          {items.map((it) => {
            const scene = what === "scenes";
            const src = scene ? (it as SceneItem).screenshot_url : (it as ImageItem).thumbnail_url;
            return (
              <button key={it.id} type="button" title={it.title ?? ""} data-testid="source-item"
                      onClick={() => onPick(scene ? { scene_id: it.id } : { image_id: it.id })}
                      className="aspect-square overflow-hidden rounded-md bg-zinc-950 ring-accent hover:ring-2 focus:outline-none focus:ring-2">
                <img src={src} alt={it.title ?? ""} loading="lazy" className="h-full w-full object-cover" />
              </button>
            );
          })}
          {!items.length && <p className="col-span-full py-8 text-center text-sm text-zinc-500">Nothing found.</p>}
        </div>
      )}
      {list.hasNextPage && (
        <div className="mt-3 text-center">
          <button type="button" className="btn-ghost" disabled={list.isFetchingNextPage} onClick={() => list.fetchNextPage()}>
            {list.isFetchingNextPage ? "Loading…" : "Load more"}
          </button>
        </div>
      )}
    </div>
  );
}

/** Admins: browse the read-only media store and pick any image file. */
function ServerSource({ onPick }: { onPick: (path: string) => void }) {
  const [cwd, setCwd] = useState<string | undefined>();
  const q = useQuery({
    queryKey: ["browse-images", cwd],
    queryFn: () => api<BrowseResult & { files: { name: string; path: string; size: number }[] }>(
      "/libraries/browse", { query: { path: cwd, images: true } }),
    retry: false,
  });
  if (q.isPending) return <Spinner className="my-10" />;
  if (q.isError) return <ErrorBox error={q.error} />;
  const dir = q.data;
  return (
    <div>
      <div className="mb-2 flex items-center gap-2 text-sm">
        <button type="button" className="btn-ghost py-1" disabled={!dir.parent} onClick={() => dir.parent && setCwd(dir.parent)}>↰ Up</button>
        <code className="truncate text-xs text-zinc-400" data-testid="server-path">{dir.path}</code>
      </div>
      <div className="max-h-[50vh] overflow-y-auto rounded-md border border-zinc-800 bg-zinc-950 p-2">
        <ul>
          {dir.entries.map((e) => (
            <li key={e.path}>
              <button type="button" onClick={() => setCwd(e.path)} aria-label={`Open ${e.name}`}
                      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-zinc-900">
                <span aria-hidden className="text-amber-400/80">📁</span>{e.name}
              </button>
            </li>
          ))}
        </ul>
        {dir.files.length > 0 && (
          <div className="mt-2 grid grid-cols-[repeat(auto-fill,minmax(110px,1fr))] gap-2">
            {dir.files.map((f) => (
              <button key={f.path} type="button" title={f.name} onClick={() => onPick(f.path)} data-testid="server-file"
                      className="overflow-hidden rounded-md bg-zinc-900 text-left ring-accent hover:ring-2 focus:outline-none focus:ring-2">
                <img src={buildUrl("/libraries/browse/preview", { path: f.path })} alt="" loading="lazy" className="aspect-square w-full object-cover" />
                <span className="block truncate px-1.5 py-1 text-[11px] text-zinc-400">{f.name}</span>
              </button>
            ))}
          </div>
        )}
        {!dir.entries.length && !dir.files.length && <p className="py-6 text-center text-sm text-zinc-500">No folders or images here.</p>}
      </div>
    </div>
  );
}
