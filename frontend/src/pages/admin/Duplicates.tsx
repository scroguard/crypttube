import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api, buildUrl } from "../../api/client";
import { useWrite } from "../../api/hooks";
import { ErrorBox, Spinner, Tabs } from "../../components/ui";
import { formatBytes, formatDuration } from "../../lib/format";
import AdminNav from "./AdminNav";

type Kind = "scene" | "image";
interface Item {
  id: number; title: string | null; path: string; rating: number | null; studio: string | null;
  tags: string[]; performers: string[]; duration: number | null; width: number | null; height: number | null;
  thumbnail_url: string; keep: boolean;
}
interface Group { oshash: string; size: number; in_sync: boolean; items: Item[] }
interface Summary { groups: number; extra_copies: number; extra_bytes: number }
interface Result { groups: Group[]; has_more: boolean; summary: Record<Kind, Summary> }

const PAGE = 25;

export default function Duplicates() {
  const [kind, setKind] = useState<Kind>("scene");
  const list = useInfiniteQuery({
    queryKey: ["duplicates", kind],
    queryFn: ({ pageParam }) => api<Result>("/duplicates", { query: { kind, limit: PAGE, offset: pageParam } }),
    initialPageParam: 0,
    getNextPageParam: (last, pages) => (last.has_more ? pages.length * PAGE : undefined),
  });
  const combine = useWrite((id: number | null) => api<{ groups: number }>("/duplicates/combine", { json: { kind, id } }));
  const summary = list.data?.pages[0]?.summary;
  const groups = list.data?.pages.flatMap((p) => p.groups) ?? [];
  const s = summary?.[kind];
  const tabs = {
    scene: `Scenes${summary ? ` (${summary.scene.groups})` : ""}`,
    image: `Images${summary ? ` (${summary.image.groups})` : ""}`,
  };
  const noun = kind === "scene" ? "scenes" : "images";
  return (
    <div className="mx-auto max-w-5xl">
      <AdminNav />
      <h1 className="mb-1 text-xl font-semibold">Duplicates</h1>
      <p className="mb-4 text-sm text-zinc-400">
        Files with identical content under different names, found by the fingerprint taken when they were indexed.
        The media store is read-only, so nothing is deleted here: combine the details so no copy holds anything the
        others lack, download the list of extra copies, delete those on the server, then run
        {" "}<Link to="/admin/maintenance" className="text-accent hover:underline">Maintenance → Clean up</Link>.
      </p>
      <Tabs tabs={tabs} value={kind} onChange={setKind} />
      {list.isPending ? <Spinner /> : list.isError ? <ErrorBox error={list.error} /> : (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-3" data-testid="duplicates-summary">
            <span className="mr-auto text-sm text-zinc-300">
              {s && s.groups > 0
                ? <>{s.groups} sets of duplicates · {s.extra_copies} extra copies · {formatBytes(s.extra_bytes)} could be freed</>
                : <>No duplicate {noun} found.</>}
            </span>
            {s && s.groups > 0 && (
              <>
                <button className="btn-ghost" disabled={combine.isPending}
                        onClick={() => confirm(`Combine the details of all ${s.groups} sets? Every copy gets the tags and performers of the others; empty fields are filled in.`) && combine.mutate(null)}>
                  Combine details for all
                </button>
                <a className="btn-primary" href={buildUrl("/duplicates/extra-copies.txt", { kind })} download>
                  Download list of extra copies
                </a>
              </>
            )}
          </div>
          {combine.error && <ErrorBox error={combine.error} className="mb-3" />}
          <ul className="space-y-4">
            {groups.map((g) => (
              <li key={`${g.oshash}-${g.size}`} className="card p-4" data-testid="duplicate-group">
                <div className="mb-3 flex flex-wrap items-center gap-3 text-sm">
                  <span className="font-medium">{g.items.length} copies</span>
                  <span className="text-zinc-400">{formatBytes(g.size)} each</span>
                  <span className="mr-auto" />
                  {g.in_sync ? <span className="text-xs text-emerald-400">Details match</span> : (
                    <>
                      <span className="text-xs text-amber-300">Details differ</span>
                      <button className="btn-ghost py-1" disabled={combine.isPending} onClick={() => combine.mutate(g.items[0].id)}>
                        Combine details
                      </button>
                    </>
                  )}
                </div>
                <ul className="space-y-2">
                  {g.items.map((i) => (
                    <li key={i.id} className="flex items-center gap-3" data-testid="duplicate-item">
                      <img src={i.thumbnail_url} alt="" loading="lazy" className="h-14 w-24 shrink-0 rounded bg-zinc-950 object-cover" />
                      <div className="min-w-0 flex-1 text-sm">
                        <div className="flex flex-wrap items-center gap-2">
                          {kind === "scene"
                            ? <Link to={`/scenes/${i.id}`} className="truncate font-medium hover:text-accent-hover">{i.title || `Scene ${i.id}`}</Link>
                            : <span className="truncate font-medium">{i.title || `Image ${i.id}`}</span>}
                          {i.keep ? <span className="chip bg-emerald-900/50 text-emerald-200">Suggested to keep</span>
                            : <span className="chip">Extra copy</span>}
                        </div>
                        <code className="block break-all text-xs text-zinc-400">{i.path}</code>
                        <div className="text-xs text-zinc-500">
                          {[i.width && i.height ? `${i.width}×${i.height}` : null,
                            i.duration ? formatDuration(i.duration) : null,
                            i.rating != null ? `${Math.round(i.rating / 20)}★` : null,
                            i.studio,
                            i.performers.length ? i.performers.join(", ") : null,
                            i.tags.length ? `${i.tags.length} tags` : null].filter(Boolean).join(" · ")}
                        </div>
                      </div>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
          {list.hasNextPage && (
            <div className="mt-4 text-center">
              <button className="btn-ghost" disabled={list.isFetchingNextPage} onClick={() => list.fetchNextPage()}>
                {list.isFetchingNextPage ? "Loading…" : "Load more"}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
