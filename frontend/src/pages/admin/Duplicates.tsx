import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { api, ApiError, buildUrl } from "../../api/client";
import { useWrite } from "../../api/hooks";
import { ErrorBox, Spinner, Tabs } from "../../components/ui";
import { formatBytes, formatDateTime, formatDuration } from "../../lib/format";
import AdminNav from "./AdminNav";

type Kind = "scene" | "image";
interface Item {
  id: number; title: string | null; path: string; rating: number | null; studio: string | null;
  tags: string[]; performers: string[]; duration: number | null; width: number | null; height: number | null;
  thumbnail_url: string; keep: boolean;
}
interface Group { oshash: string; size: number; in_sync: boolean; items: Item[] }
interface Summary { groups: number; extra_copies: number; extra_bytes: number }
interface SetResult {
  deleted: number; kept: number; reason: string | null; suggested_kept: boolean;
  keeper: { id: number; path: string }; kept_copies: { id: number; path: string; reason: string }[];
}
interface DeleteJob { id: number; status: string; progress: number | null; message: string | null; error: string | null; finished_at: string | null }
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
  // Deleting one copy is refused when it differs from every other copy; the admin can then
  // look at why and delete it anyway.
  const remove = useWrite(async (id: number) => {
    try {
      return await api("/duplicates/delete", { json: { kind, id } });
    } catch (e) {
      if (!(e instanceof ApiError) || e.status !== 409 || !e.message.includes("not identical")) throw e;
      const why = e.message.match(/\((.*)\)$/)?.[1] ?? "they differ";
      if (!confirm(`This copy is not identical to the others: ${why}.\n\nThey may still be the same video (one of them can be damaged). Delete this copy anyway? This cannot be undone.`)) return null;
      return await api("/duplicates/delete", { json: { kind, id, force: true } });
    }
  });
  const removeSet = useWrite(({ id, force = false, keeper_id }: { id: number; force?: boolean; keeper_id?: number }) =>
    api<SetResult>("/duplicates/delete-set", { json: { kind, id, force, keeper_id } }));
  const [override, setOverride] = useState(false);
  const removeAll = useWrite(async () => {
    const r = await api<{ job_id: number }>("/duplicates/delete-extras", { json: { kind, force: override } });
    await deleteJob.refetch();
    return r;
  });
  // The background "delete all extra copies" job: its progress and result, right here.
  const deleteJob = useQuery({
    queryKey: ["duplicates-job", kind],
    queryFn: () => api<DeleteJob | null>("/duplicates/job", { query: { kind } }),
    refetchInterval: (q) => (q.state.data && ["queued", "running"].includes(q.state.data.status) ? 2000 : false),
  });
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
                        onClick={() => confirm(override
                          ? `Delete all ${s.extra_copies} extra copies from disk, INCLUDING copies that are not identical to the one kept? One copy of each set stays. This cannot be undone.`
                          : `Delete all ${s.extra_copies} extra copies from disk? One copy of each set is kept (normally the one marked "Suggested to keep"); copies that differ from it are left alone. This cannot be undone.`)
                          && removeAll.mutate(undefined)}>
                  Delete all extra copies…
                </button>
                <label className="flex items-center gap-2 text-xs text-zinc-400" title="Admin override: the byte-for-byte check still picks which copy to keep, but copies that differ are deleted too">
                  <input type="checkbox" className="accent-accent" checked={override} onChange={(e) => setOverride(e.target.checked)} />
                  Also delete copies that differ
                </label>
              </>
            )}
          </div>
          {(combine.error || remove.error || removeSet.error || removeAll.error) && <ErrorBox error={combine.error || remove.error || removeSet.error || removeAll.error} className="mb-3" />}
          {deleteJob.data && <DeleteJobStatus job={deleteJob.data} onDone={() => list.refetch()} />}
          {removeSet.data && !removeSet.isPending && (
            <div className="mb-3 rounded-md border border-zinc-800 bg-zinc-950/40 px-4 py-3 text-sm" role="status" data-testid="set-result">
              <p>
                Deleted {removeSet.data.deleted} {removeSet.data.deleted === 1 ? "copy" : "copies"}; kept
                {" "}<code className="break-all text-xs">{removeSet.data.keeper.path}</code>
                {!removeSet.data.suggested_kept && " (the copy most others matched, instead of the suggested one)"}.
              </p>
              {removeSet.data.kept_copies.map((k) => (
                <p key={k.id} className="mt-1 text-amber-300">
                  Also kept, because it differs: <code className="break-all text-xs">{k.path}</code> ({k.reason}).
                </p>
              ))}
              {removeSet.data.kept_copies.length > 0 && (
                <button className="btn-danger mt-2 py-1" disabled={removeSet.isPending}
                        onClick={() => confirm(`Delete ${removeSet.data!.kept_copies.length === 1 ? "that copy" : `those ${removeSet.data!.kept_copies.length} copies`} anyway, keeping only ${removeSet.data!.keeper.path}? They are not identical to it. This cannot be undone.`)
                          && removeSet.mutate({ id: removeSet.data!.keeper.id, keeper_id: removeSet.data!.keeper.id, force: true })}>
                  Delete {removeSet.data.kept_copies.length === 1 ? "it" : "them"} anyway
                </button>
              )}
            </div>
          )}
          <ul className="space-y-4">
            {groups.map((g) => (
              <li key={`${g.oshash}-${g.size}`} className="card p-4" data-testid="duplicate-group">
                <div className="mb-3 flex flex-wrap items-center gap-3 text-sm">
                  <span className="font-medium">{g.items.length} copies</span>
                  <span className="text-zinc-400">{formatBytes(g.size)} each</span>
                  <span className="mr-auto" />
                  <button className="btn-danger py-1" disabled={removeSet.isPending}
                          onClick={() => confirm(`Delete ${g.items.length - 1} of these ${g.items.length} copies from disk? One copy stays (normally the one marked "Suggested to keep"), with the details of all of them. A copy that differs from the others is left alone. This cannot be undone.`) && removeSet.mutate({ id: g.items[0].id })}>
                    {removeSet.isPending && removeSet.variables?.id === g.items[0].id ? "Deleting…" : `Delete ${g.items.length - 1} extra ${g.items.length === 2 ? "copy" : "copies"}`}
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

function DeleteJobStatus({ job, onDone }: { job: DeleteJob; onDone: () => void }) {
  const active = job.status === "queued" || job.status === "running";
  const cancel = useWrite(() => api(`/jobs/${job.id}/cancel`, { method: "POST" }));
  const wasActive = useRef(active);
  useEffect(() => {
    if (wasActive.current && !active) onDone();   // finished: show what's left
    wasActive.current = active;
  }, [active, onDone]);
  const color = job.status === "failed" ? "border-red-900 bg-red-950/40 text-red-200"
    : active ? "border-sky-900 bg-sky-950/40 text-sky-200" : "border-zinc-800 bg-zinc-950/40 text-zinc-300";
  return (
    <div className={`mb-3 rounded-md border px-4 py-3 text-sm ${color}`} role="status" data-testid="delete-job">
      <div className="flex flex-wrap items-center gap-3">
        <span className="font-medium">
          {job.status === "queued" ? "Delete all extra copies: waiting to start (scans and other disk jobs go one at a time)…"
            : job.status === "running" ? "Deleting extra copies…"
            : job.status === "failed" ? "Deleting extra copies failed"
            : job.status === "cancelled" ? "Deleting extra copies was stopped"
            : `Last run${job.finished_at ? ` (${formatDateTime(job.finished_at)})` : ""}`}
        </span>
        {active && (
          <button className="btn-ghost ml-auto py-1" disabled={cancel.isPending} onClick={() => cancel.mutate(undefined)}>Stop</button>
        )}
      </div>
      {job.status === "running" && job.progress != null && (
        <div className="mt-2 h-1.5 rounded bg-zinc-800"><div className="h-full rounded bg-sky-500" style={{ width: `${job.progress * 100}%` }} /></div>
      )}
      {(job.error || job.message) && <p className="mt-1 whitespace-pre-line text-xs opacity-90">{job.error || job.message}</p>}
    </div>
  );
}
