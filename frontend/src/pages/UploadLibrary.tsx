import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { api } from "../api/client";
import { useWrite } from "../api/hooks";
import type { LibraryFile } from "../api/types";
import { Tabs } from "../components/EntityDialogs";
import EntityContent from "../components/EntityContent";
import LibrarySettings, { VisibilityBadge } from "../components/LibrarySettings";
import { Empty, ErrorBox, Spinner } from "../components/ui";
import { formatBytes, plural } from "../lib/format";
import { filesFromDrop, filesFromInput, isMediaFile, joinPath, uploadFile, type PendingFile } from "../lib/upload";
import { QuotaBar, useMyLibraries } from "./Uploads";

interface QueueItem extends PendingFile {
  key: number;
  status: "waiting" | "uploading" | "done" | "error" | "skipped";
  progress: number;
  error?: string;
}

let nextKey = 1;

/** Sequential upload queue with per-file progress. */
function useUploader(libraryId: number) {
  const qc = useQueryClient();
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const running = useRef(false);
  const items = useRef<QueueItem[]>([]);
  items.current = queue;

  const patch = (key: number, change: Partial<QueueItem>) =>
    setQueue((q) => q.map((it) => (it.key === key ? { ...it, ...change } : it)));

  const refresh = useCallback(() => qc.invalidateQueries({ predicate: (q) => q.queryKey[0] !== "auth" }), [qc]);

  const pump = useCallback(async () => {
    if (running.current) return;
    running.current = true;
    for (;;) {
      const next = items.current.find((it) => it.status === "waiting");
      if (!next) break;
      patch(next.key, { status: "uploading" });
      next.status = "uploading";
      try {
        await uploadFile(libraryId, next, (progress) => patch(next.key, { progress })).promise;
        patch(next.key, { status: "done", progress: 1 });
        next.status = "done";
      } catch (e) {
        patch(next.key, { status: "error", error: e instanceof Error ? e.message : String(e) });
        next.status = "error";
      }
      // let React flush so items.current reflects newly added files
      await new Promise((r) => setTimeout(r, 0));
    }
    running.current = false;
    // The server indexes uploads in the background; refresh now and again shortly after.
    refresh();
    setTimeout(refresh, 3000);
    setTimeout(refresh, 9000);
  }, [libraryId, refresh]);

  const add = useCallback((files: PendingFile[], folder: string) => {
    const added: QueueItem[] = files.map((f) => {
      const path = joinPath(folder, f.path);
      const ok = isMediaFile(f.file.name);
      return { ...f, path, key: nextKey++, progress: 0, status: ok ? "waiting" : "skipped",
               error: ok ? undefined : "not a video, image or zip" };
    });
    setQueue((q) => [...q, ...added]);
    items.current = [...items.current, ...added];
    pump();
  }, [pump]);

  const busy = queue.some((it) => it.status === "waiting" || it.status === "uploading");
  return { queue, add, busy, clear: () => setQueue((q) => q.filter((it) => it.status === "waiting" || it.status === "uploading")) };
}

export default function UploadLibrary() {
  const id = Number(useParams().id);
  const navigate = useNavigate();
  const libs = useMyLibraries();
  const [tab, setTab] = useState<"content" | "files">("content");
  const [settings, setSettings] = useState(false);
  const [folder, setFolder] = useState("");
  const [dragging, setDragging] = useState(false);
  const uploader = useUploader(id);
  const del = useWrite(() => api(`/my/libraries/${id}`, { method: "DELETE" }));
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);

  // Warn before leaving mid-upload.
  useEffect(() => {
    if (!uploader.busy) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [uploader.busy]);

  if (libs.isPending) return <Spinner className="mt-24" />;
  if (libs.isError) return <ErrorBox error={libs.error} />;
  const lib = libs.data.libraries.find((l) => l.id === id);
  if (!lib) return <Empty>Library not found.</Empty>;

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Link to="/uploads" className="text-sm text-zinc-400 hover:text-white">← My uploads</Link>
        <h1 className="text-2xl font-semibold">{lib.name}</h1>
        <VisibilityBadge library={lib} />
        <span className="mr-auto text-sm text-zinc-500">
          {plural(lib.scene_count, "video")} · {plural(lib.image_count, "image")} · {formatBytes(lib.bytes)}
        </span>
        <QuotaBar used={libs.data.used_bytes} quota={libs.data.quota_bytes} />
        <button className="btn-ghost" onClick={() => setSettings(true)}>Sharing & settings</button>
        <button className="btn-danger" disabled={uploader.busy || del.isPending} onClick={() => {
          if (confirm(`Delete “${lib.name}” and all ${lib.file_count} files in it? This cannot be undone.`))
            del.mutate(undefined, { onSuccess: () => navigate("/uploads", { replace: true }) });
        }}>Delete library</button>
      </div>

      <div data-testid="dropzone"
           onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
           onDragLeave={() => setDragging(false)}
           onDrop={async (e) => { e.preventDefault(); setDragging(false); uploader.add(await filesFromDrop(e.dataTransfer), folder); }}
           className={`mb-4 rounded-lg border-2 border-dashed p-6 text-center transition-colors ${dragging ? "border-accent bg-accent/10" : "border-zinc-700"}`}>
        <p className="text-sm text-zinc-300">Drop videos, images, zip archives or whole folders here</p>
        <div className="mt-3 flex flex-wrap items-center justify-center gap-2">
          <button className="btn-primary" onClick={() => fileInput.current?.click()}>Choose files</button>
          <button className="btn-ghost" onClick={() => folderInput.current?.click()}>Choose a folder</button>
          <input className="input w-56" placeholder="Into subfolder (optional)" value={folder}
                 onChange={(e) => setFolder(e.target.value)} aria-label="Destination subfolder" />
        </div>
        <p className="mt-2 text-xs text-zinc-500">A folder of images becomes a gallery. Uploads are indexed automatically.</p>
        <input ref={fileInput} type="file" multiple hidden data-testid="file-input"
               onChange={(e) => { if (e.target.files) uploader.add(filesFromInput(e.target.files), folder); e.target.value = ""; }} />
        <input ref={folderInput} type="file" multiple hidden
               {...({ webkitdirectory: "" } as Record<string, string>)}
               onChange={(e) => { if (e.target.files) uploader.add(filesFromInput(e.target.files), folder); e.target.value = ""; }} />
      </div>

      {uploader.queue.length > 0 && (
        <div className="card mb-4 max-h-64 overflow-y-auto p-3" data-testid="upload-queue">
          <div className="mb-2 flex items-center text-sm">
            <span className="mr-auto font-medium">
              {uploader.busy ? "Uploading…" : "Uploads finished"} ({uploader.queue.filter((q) => q.status === "done").length}/{uploader.queue.length})
            </span>
            {!uploader.busy && <button className="text-xs text-zinc-400 underline" onClick={uploader.clear}>clear</button>}
          </div>
          <ul className="space-y-1.5">
            {uploader.queue.map((it) => (
              <li key={it.key} className="text-xs" data-testid="upload-item" data-status={it.status}>
                <div className="flex gap-2">
                  <span className="mr-auto truncate font-mono">{it.path}</span>
                  <span className="text-zinc-500">{formatBytes(it.file.size)}</span>
                  <span className={it.status === "error" || it.status === "skipped" ? "text-red-400" : it.status === "done" ? "text-emerald-400" : "text-zinc-400"}>
                    {it.status === "uploading" ? `${Math.round(it.progress * 100)}%` : it.error ?? it.status}
                  </span>
                </div>
                {it.status === "uploading" && (
                  <div className="mt-0.5 h-1 rounded bg-zinc-800"><div className="h-full rounded bg-accent" style={{ width: `${it.progress * 100}%` }} /></div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <Tabs tabs={{ content: "Content", files: `Files (${lib.file_count})` }} value={tab} onChange={setTab} />
      {tab === "content" ? <EntityContent filter={{ library_ids: [id] }} /> : <FileList libraryId={id} />}
      {settings && <LibrarySettings library={lib} onClose={() => setSettings(false)} />}
      {del.isError && <ErrorBox error={del.error} className="mt-3" />}
    </div>
  );
}

function FileList({ libraryId }: { libraryId: number }) {
  const files = useQuery({
    queryKey: ["my-library-files", libraryId],
    queryFn: () => api<LibraryFile[]>(`/my/libraries/${libraryId}/files`),
  });
  const del = useWrite((path: string) => api(`/my/libraries/${libraryId}/files`, { method: "DELETE", query: { path } }));
  if (files.isPending) return <Spinner className="mt-10" />;
  if (files.isError) return <ErrorBox error={files.error} />;
  if (!files.data.length) return <Empty>No files yet.</Empty>;
  return (
    <div className="card divide-y divide-zinc-800">
      {files.data.map((f) => (
        <div key={f.path} className="flex items-center gap-3 px-4 py-2 text-sm" data-testid="library-file">
          {f.scene_id
            ? <Link to={`/scenes/${f.scene_id}`} className="mr-auto truncate font-mono hover:text-accent-hover">{f.path}</Link>
            : <span className="mr-auto truncate font-mono">{f.path}</span>}
          {!f.indexed && <span className="chip bg-amber-950 text-amber-300" title="Still being processed, or the file couldn't be read as media">not indexed</span>}
          <span className="text-xs text-zinc-500">{formatBytes(f.size)}</span>
          <button className="btn-ghost py-1 text-xs" disabled={del.isPending}
                  onClick={() => confirm(`Delete ${f.path}?`) && del.mutate(f.path)}>Delete</button>
        </div>
      ))}
      {del.isError && <ErrorBox error={del.error} className="m-3" />}
    </div>
  );
}
