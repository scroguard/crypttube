import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router";
import { api } from "../../api/client";
import { useWrite } from "../../api/hooks";
import type { Library, UserLibrarySummary } from "../../api/types";
import { formatBytes } from "../../lib/format";
import DirectoryPicker from "../../components/DirectoryPicker";
import { ErrorBox, Spinner, Toggle } from "../../components/ui";
import AdminNav from "./AdminNav";

export default function Libraries() {
  const libs = useQuery({
    queryKey: ["libraries"],
    queryFn: () => api<Library[]>("/libraries"),
    // Watcher startup happens in the background; poll until it settles.
    refetchInterval: (q) => (q.state.data?.some((l) => l.watch && l.watch_state !== "active" && l.watch_state !== "error") ? 2000 : false),
  });
  const [path, setPath] = useState("/media/");
  const [notice, setNotice] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const [scanIn, setScanIn] = useState<Library | null>(null);
  const add = useWrite((p: string) => api<Library>("/libraries", { json: { path: p, watch: true } }));
  const patch = useWrite(({ id, ...body }: { id: number } & Partial<Library>) => api(`/libraries/${id}`, { method: "PATCH", json: body }));
  const del = useWrite((id: number) => api(`/libraries/${id}`, { method: "DELETE" }));
  const scan = useWrite(({ id, path }: { id: number | null; path?: string }) =>
    api<{ job_id: number }>(id ? `/libraries/${id}/scan` : "/libraries/scan", path ? { json: { path } } : { method: "POST" }));
  const generate = useWrite((body: { force?: boolean; library_id?: number }) =>
    api<{ job_id: number }>("/generate", { json: body }));
  const queued = (r: { job_id: number }) => setNotice(`Queued job #${r.job_id}.`);

  function submit(e: FormEvent) {
    e.preventDefault();
    add.mutate(path.trim(), { onSuccess: (lib) => { setPath("/media/"); scan.mutate({ id: lib.id }, { onSuccess: queued }); } });
  }

  return (
    <div className="mx-auto max-w-4xl">
      <AdminNav />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <h1 className="mr-auto text-xl font-semibold">Libraries</h1>
        <button className="btn-ghost" onClick={() => scan.mutate({ id: null }, { onSuccess: queued })}>Scan all</button>
        <button className="btn-ghost" title="Create thumbnails and scrub previews that are missing or unreadable"
                onClick={() => generate.mutate({}, { onSuccess: queued })}>Generate missing previews</button>
        <button className="btn-ghost" title="Discard every generated thumbnail and preview and build them again"
                onClick={() => confirm("Rebuild all thumbnails and previews for every library? Existing ones are discarded first; this can take a while on a large library.")
                  && generate.mutate({ force: true }, { onSuccess: queued })}>Rebuild all previews…</button>
      </div>
      {notice && <p className="mb-3 text-sm text-emerald-400">{notice} <Link to="/admin/jobs" className="underline">View jobs</Link></p>}
      <form onSubmit={submit} className="card mb-6 flex flex-wrap gap-2 p-4">
        <input className="input flex-1 font-mono" value={path} onChange={(e) => setPath(e.target.value)} aria-label="Library path" />
        <button type="button" className="btn-ghost" onClick={() => setPicking(true)}>Browse…</button>
        <button className="btn-primary" disabled={add.isPending}>Add library</button>
        <p className="w-full text-xs text-zinc-500">Paths are as seen inside the container: your media folder is mounted read-only at /media.</p>
        {add.isError && <ErrorBox error={add.error} className="w-full" />}
      </form>
      {libs.isPending ? <Spinner /> : libs.isError ? <ErrorBox error={libs.error} /> : (
        <ul className="space-y-3">
          {libs.data.map((lib) => (
            <li key={lib.id} className="card p-4" data-testid="library">
              <div className="flex flex-wrap items-center gap-2">
                <code className="break-all text-sm">{lib.path}</code>
                <WatchBadge lib={lib} />
                <span className="mr-auto" />
                <button className="btn-ghost py-1" onClick={() => scan.mutate({ id: lib.id }, { onSuccess: queued })}>Scan</button>
                <button className="btn-ghost py-1" onClick={() => setScanIn(lib)}>Scan folder…</button>
                <button className="btn-ghost py-1" title="Discard and rebuild this library's thumbnails and previews"
                        onClick={() => confirm(`Rebuild all thumbnails and previews for ${lib.path}?`)
                          && generate.mutate({ force: true, library_id: lib.id }, { onSuccess: queued })}>Rebuild previews</button>
                <button className="btn-danger py-1" onClick={() => confirm(`Remove ${lib.path}? Indexed metadata for it is deleted; files are untouched.`) && del.mutate(lib.id)}>Remove</button>
              </div>
              <div className="mt-3 flex flex-wrap gap-5">
                <Toggle checked={lib.enabled} onChange={(v) => patch.mutate({ id: lib.id, enabled: v })} label="Enabled" />
                <Toggle checked={lib.watch} onChange={(v) => patch.mutate({ id: lib.id, watch: v })} label="Watch for changes" />
                <Toggle checked={lib.create_folder_galleries} onChange={(v) => patch.mutate({ id: lib.id, create_folder_galleries: v })} label="Folder galleries" />
              </div>
              {lib.watch && lib.watch_state === "error" && lib.watch_error && (
                <p className="mt-2 text-xs text-red-300">Change watching is off for this library: {lib.watch_error}. Scans still work.</p>
              )}
              <ExcludeEditor lib={lib} onSave={(patterns) => patch.mutate({ id: lib.id, exclude_patterns: patterns })} />
            </li>
          ))}
        </ul>
      )}
      {(patch.error || del.error || scan.error || generate.error) && <ErrorBox error={patch.error || del.error || scan.error || generate.error} className="mt-3" />}

      <UserLibraries onDelete={(id) => del.mutate(id)} />

      {picking && (
        <DirectoryPicker
          title="Choose a library folder"
          start={path.trim().replace(/\/+$/, "") || undefined}
          selectLabel="Use this folder"
          blocked={(d) => d.library ? `Already part of the library ${d.library.path}`
            : d.entries.some((e) => e.library_id) ? "Contains an existing library; libraries can't overlap" : null}
          onSelect={(p) => { setPath(p); setPicking(false); }}
          onClose={() => setPicking(false)}
        />
      )}
      {scanIn && (
        <DirectoryPicker
          title="Scan a single folder"
          start={scanIn.path}
          floor={scanIn.path}
          selectLabel="Scan this folder"
          onSelect={(p) => { scan.mutate({ id: scanIn.id, path: p }, { onSuccess: queued }); setScanIn(null); }}
          onClose={() => setScanIn(null)}
        />
      )}
    </div>
  );
}

/** Storage overview of users' own libraries. Admins can remove them, not look inside. */
function UserLibraries({ onDelete }: { onDelete: (id: number) => void }) {
  const q = useQuery({ queryKey: ["user-libraries"], queryFn: () => api<UserLibrarySummary[]>("/libraries/user") });
  if (!q.data?.length) return null;
  return (
    <section className="mt-10">
      <h2 className="mb-1 text-lg font-semibold">User libraries</h2>
      <p className="mb-3 text-sm text-zinc-400">
        Uploaded by users into their own libraries. Their content is only visible to the people the owner shares it with.
      </p>
      <div className="card divide-y divide-zinc-800">
        {q.data.map((l) => (
          <div key={l.id} className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm" data-testid="user-library">
            <span className="font-medium">{l.owner.username}</span>
            <span className="mr-auto text-zinc-400">{l.name}</span>
            <span className="chip">{l.visibility === "private" ? "Private" : l.visibility === "everyone" ? "Everyone" : "Selected users"}</span>
            <span className="w-40 text-right text-zinc-400">{l.file_count} files · {formatBytes(l.bytes)}</span>
            <button className="btn-danger py-1" onClick={() => confirm(`Delete ${l.owner.username}'s library “${l.name}” and its ${l.file_count} uploaded files?`) && onDelete(l.id)}>Delete</button>
          </div>
        ))}
      </div>
    </section>
  );
}

function WatchBadge({ lib }: { lib: Library }) {
  if (!lib.enabled || !lib.watch) return null;
  if (lib.watch_state === "active") return <span className="chip bg-emerald-950 text-emerald-300" data-testid="watch-state">Watching</span>;
  if (lib.watch_state === "error")
    return <span className="chip bg-red-950 text-red-300" data-testid="watch-state" title={lib.watch_error ?? ""}>Watch failed</span>;
  return (
    <span className="chip bg-amber-950 text-amber-300" data-testid="watch-state"
          title="Registering every folder for change notifications; scans and other jobs keep running meanwhile">
      Starting watcher…
    </span>
  );
}

function ExcludeEditor({ lib, onSave }: { lib: Library; onSave: (p: string[]) => void }) {
  const [text, setText] = useState(lib.exclude_patterns.join(", "));
  const dirty = text !== lib.exclude_patterns.join(", ");
  return (
    <div className="mt-3 flex gap-2">
      <input className="input font-mono text-xs" placeholder="Exclude patterns, e.g. */trash/*, *.part" value={text} onChange={(e) => setText(e.target.value)} aria-label="Exclude patterns" />
      {dirty && <button className="btn-primary py-1" onClick={() => onSave(text.split(",").map((s) => s.trim()).filter(Boolean))}>Save</button>}
    </div>
  );
}
