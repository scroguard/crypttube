import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { useWrite } from "../api/hooks";
import { ErrorBox, Modal, Spinner } from "./ui";

interface Link {
  id: number; path: string; library: string; relative: string;
  watching: boolean; missing: boolean; other_studios: number;
}
interface Listing {
  path: string | null;
  parent: string | null;
  library: { name: string; path: string; mine: boolean } | null;
  relative?: string;
  entries: { name: string; path: string; mine?: boolean }[];
}

/**
 * The folders linked to a performer or studio: files that arrive in one get tagged with
 * them. Shown to people who can tag some library; the server checks every folder again.
 */
export default function FolderLinks({ kind, id }: { kind: "performers" | "studios"; id: number }) {
  const q = useQuery({
    queryKey: ["detail", "folder-links", kind, id],
    queryFn: () => api<{ can_link: boolean; links: Link[] }>(`/folder-links/${kind}/${id}`),
    retry: false,
  });
  const [adding, setAdding] = useState(false);
  const remove = useWrite((linkId: number) => api(`/folder-links/${kind}/${id}/${linkId}`, { method: "DELETE" }));
  if (!q.data?.can_link) return null;
  const links = q.data.links;
  const who = kind === "performers" ? "this performer" : "this studio";

  return (
    <section className="mt-4 text-sm" data-testid="folder-links">
      <div className="flex flex-wrap items-center gap-3">
        <span className="label">Watched folders</span>
        <button className="btn-ghost py-1" onClick={() => setAdding(true)}>Add folder</button>
      </div>
      {links.length > 0 && (
        <ul className="mt-2 space-y-1">
          {links.map((l) => (
            <li key={l.id} className="flex flex-wrap items-center gap-2">
              <code className="min-w-0 break-all text-xs text-text-muted" title={l.path}>
                {l.library}{l.relative === "." ? "" : ` / ${l.relative}`}
              </code>
              {l.missing && <span className="chip text-warning">folder not found</span>}
              {!l.missing && !l.watching && (
                <span className="chip" title="This library isn't watched, so new files are tagged at its next scan">tagged at next scan</span>
              )}
              {l.other_studios > 0 && (
                <span className="chip text-warning" title="A file has one studio: the one linked to the deepest folder is used, then the one linked first">
                  other studios linked here
                </span>
              )}
              <button className="ml-auto text-text-subtle hover:text-text" aria-label={`Stop watching ${l.relative}`}
                      disabled={remove.isPending} onClick={() => remove.mutate(l.id)}>✕</button>
            </li>
          ))}
        </ul>
      )}
      {remove.isError && <ErrorBox error={remove.error} />}
      {adding && <AddFolderDialog kind={kind} id={id} who={who} onClose={() => setAdding(false)} />}
    </section>
  );
}

function AddFolderDialog({ kind, id, who, onClose }: {
  kind: "performers" | "studios"; id: number; who: string; onClose: () => void;
}) {
  const [cwd, setCwd] = useState<string | null>(null);
  const [scope, setScope] = useState<"new" | "all">("new");
  const listing = useQuery({
    queryKey: ["folder-link-folders", cwd],
    queryFn: () => api<Listing>("/folder-links/folders", { query: { path: cwd ?? undefined } }),
    retry: false,
  });
  const add = useWrite((body: object) => api(`/folder-links/${kind}/${id}`, { json: body }));
  const dir = listing.data;

  return (
    <Modal title="Watch a folder" onClose={onClose} wide>
      <div className="space-y-3">
        <div className="flex items-center gap-2 text-sm">
          <button className="btn-ghost py-1" disabled={cwd === null} onClick={() => setCwd(dir?.parent ?? null)}>↰ Up</button>
          <code className="truncate text-xs text-text-muted">
            {dir?.library ? `${dir.library.name}${dir.relative ? ` / ${dir.relative}` : ""}` : "Choose a library"}
          </code>
        </div>
        <div className="max-h-[40vh] overflow-y-auto rounded-md border border-border bg-bg p-2">
          {listing.isPending ? <Spinner className="my-6" /> : listing.isError ? <ErrorBox error={listing.error} /> : (
            <ul>
              {dir!.entries.map((e) => (
                <li key={e.path}>
                  <button type="button" onClick={() => setCwd(e.path)} aria-label={`Open ${e.name}`}
                          className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-surface">
                    <span aria-hidden className="text-warning/80">📁</span>
                    <span className="truncate">{e.name}</span>
                    {e.mine && <span className="chip ml-auto">yours</span>}
                  </button>
                </li>
              ))}
              {!dir!.entries.length && (
                <li className="py-6 text-center text-sm text-text-subtle">
                  {cwd === null ? "There's no library you can tag." : "No folders here."}
                </li>
              )}
            </ul>
          )}
        </div>
        {cwd !== null && (
          <fieldset className="space-y-1 text-sm text-text">
            <legend className="label">Tag with {who}, including subfolders</legend>
            <label className="flex items-center gap-2">
              <input type="radio" name="scope" checked={scope === "new"} onChange={() => setScope("new")} />
              New files only
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" name="scope" checked={scope === "all"} onChange={() => setScope("all")} />
              All files, including the ones already here (runs in the background)
            </label>
          </fieldset>
        )}
        {add.isError && <ErrorBox error={add.error} />}
        <div className="flex justify-end gap-2">
          <button className="btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={cwd === null || add.isPending}
                  onClick={() => add.mutate({ path: cwd, scope }, { onSuccess: onClose })}>
            {add.isPending ? "Adding…" : "Watch this folder"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
