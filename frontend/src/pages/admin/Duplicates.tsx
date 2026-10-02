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
  const remove = useWrite((id: number) => api("/duplicates/delete", { json: { kind, id } }));
  const removeSet = useWrite(async (id: number) => {
    const r = await api<{ deleted: number; kept: number; reason: string | null }>("/duplicates/delete-set", { json: { kind, id } });
    if (r.kept) throw new Error(`${r.deleted} deleted, ${r.kept} kept: ${r.reason}`);
    return r;
  });
  const removeAll = useWrite(() => api<{ job_id: number }>("/duplicates/delete-extras", { json: { kind } }));
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
        Deleting a copy first gives its tags, performers and other details to the copies that stay, and compares
        the files byte for byte; a file that isn't truly identical is never deleted. Deletion is permanent.
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
                <a className="btn-ghost" href={buildUrl("/duplicates/extra-copies.txt", { kind })} download>
                  Download list of extra copies
                </a>
                <button className="btn-danger" disabled={removeAll.isPending}
                        onClick={() => confirm(`Delete all ${s.extra_copies} extra copies from disk? One copy of each set is kept (the one marked "Suggested to keep"). This cannot be undone.`) && removeAll.mutate(undefined)}>
                  Delete all extra copies…
                </button>
              </>
            )}
          </div>
          {(combine.error || remove.error || removeSet.error || removeAll.error) && <ErrorBox error={combine.error || remove.error || removeSet.error || removeAll.error} className="mb-3" />}
          {removeAll.isSuccess && (
            <p className="mb-3 text-sm text-emerald-300" role="status">
              Deleting in the background. Follow it under <Link to="/admin/jobs" className="underline">Jobs</Link>, then reload this page.
            </p>
          )}
          <ul className="space-y-4">
            {groups.map((g) => (
              <li key={`${g.oshash}-${g.size}`} className="card p-4" data-testid="duplicate-group">
                <div className="mb-3 flex flex-wrap items-center gap-3 text-sm">
                  <span className="font-medium">{g.items.length} copies</span>
                  <span className="text-zinc-400">{formatBytes(g.size)} each</span>
                  <span className="mr-auto" />
                  <button className="btn-danger py-1" disabled={removeSet.isPending}
                          onClick={() => confirm(`Delete ${g.items.length - 1} of these ${g.items.length} copies from disk? The one marked "Suggested to keep" stays, with the details of all of them. This cannot be undone.`) && removeSet.mutate(g.items[0].id)}>
                    {removeSet.isPending && removeSet.variables === g.items[0].id ? "Deleting…" : `Delete ${g.items.length - 1} extra ${g.items.length === 2 ? "copy" : "copies"}`}
                  </button>
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
                      <button className="btn-danger shrink-0 py-1" disabled={remove.isPending} aria-label={`Delete ${i.path}`}
                              onClick={() => confirm(`Delete this copy from disk?\n\n${i.path}\n\nIts details are kept on the other ${g.items.length > 2 ? "copies" : "copy"}. This cannot be undone.`) && remove.mutate(i.id)}>
                        Delete
                      </button>
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
