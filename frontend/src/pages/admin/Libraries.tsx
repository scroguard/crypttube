import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router";
import { api } from "../../api/client";
import { useWrite } from "../../api/hooks";
import type { Library } from "../../api/types";
import DirectoryPicker from "../../components/DirectoryPicker";
import { ErrorBox, Spinner, Toggle } from "../../components/ui";
import AdminNav from "./AdminNav";

export default function Libraries() {
  const libs = useQuery({ queryKey: ["libraries"], queryFn: () => api<Library[]>("/libraries") });
  const [path, setPath] = useState("/media/");
  const [notice, setNotice] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const [scanIn, setScanIn] = useState<Library | null>(null);
  const add = useWrite((p: string) => api<Library>("/libraries", { json: { path: p, watch: true } }));
  const patch = useWrite(({ id, ...body }: { id: number } & Partial<Library>) => api(`/libraries/${id}`, { method: "PATCH", json: body }));
  const del = useWrite((id: number) => api(`/libraries/${id}`, { method: "DELETE" }));
  const scan = useWrite(({ id, path }: { id: number | null; path?: string }) =>
    api<{ job_id: number }>(id ? `/libraries/${id}/scan` : "/libraries/scan", path ? { json: { path } } : { method: "POST" }));
  const generate = useWrite(() => api<{ job_id: number }>("/generate", { method: "POST" }));
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
        <button className="btn-ghost" onClick={() => generate.mutate(undefined, { onSuccess: queued })}>Generate missing previews</button>
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
                <code className="mr-auto break-all text-sm">{lib.path}</code>
                <button className="btn-ghost py-1" onClick={() => scan.mutate({ id: lib.id }, { onSuccess: queued })}>Scan</button>
                <button className="btn-ghost py-1" onClick={() => setScanIn(lib)}>Scan folder…</button>
                <button className="btn-danger py-1" onClick={() => confirm(`Remove ${lib.path}? Indexed metadata for it is deleted; files are untouched.`) && del.mutate(lib.id)}>Remove</button>
              </div>
              <div className="mt-3 flex flex-wrap gap-5">
                <Toggle checked={lib.enabled} onChange={(v) => patch.mutate({ id: lib.id, enabled: v })} label="Enabled" />
                <Toggle checked={lib.watch} onChange={(v) => patch.mutate({ id: lib.id, watch: v })} label="Watch for changes" />
                <Toggle checked={lib.create_folder_galleries} onChange={(v) => patch.mutate({ id: lib.id, create_folder_galleries: v })} label="Folder galleries" />
              </div>
              <ExcludeEditor lib={lib} onSave={(patterns) => patch.mutate({ id: lib.id, exclude_patterns: patterns })} />
            </li>
          ))}
        </ul>
      )}
      {(patch.error || del.error || scan.error) && <ErrorBox error={patch.error || del.error || scan.error} className="mt-3" />}

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
