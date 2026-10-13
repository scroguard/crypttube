import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { useWrite } from "../api/hooks";
import { ErrorBox, Modal, Spinner } from "./ui";

interface Folder { name: string; path: string; mine?: boolean }
interface Listing {
  path: string | null;
  parent: string | null;
  library: { name: string; path: string; mine: boolean } | null;
  relative?: string;
  entries: Folder[];
  truncated?: boolean;
}
interface Conflict { id: number; name: string; suggested: string }
interface MoveResult { moved: number; dest: string; conflicts: Conflict[]; skipped: { id: number; reason: string }[] }
type OnConflict = "ask" | "rename" | "skip";

/**
 * Move selected scenes or images to another folder (the server allows only libraries the
 * user may change). A new folder can be created on the way; name clashes are resolved by
 * asking, numbering, or skipping.
 */
export default function MoveDialog({ kind, ids, onClose, onMoved }: {
  kind: "scenes" | "images"; ids: number[]; onClose: () => void; onMoved: () => void;
}) {
  const [cwd, setCwd] = useState<string | null>(null);
  const [newFolder, setNewFolder] = useState("");
  const [onConflict, setOnConflict] = useState<OnConflict>("ask");
  const [summary, setSummary] = useState<{ moved: number; skipped: MoveResult["skipped"]; dest: string } | null>(null);
  const [conflicts, setConflicts] = useState<{ dest: string; items: (Conflict & { value: string })[] } | null>(null);

  const listing = useQuery({
    queryKey: ["move-folders", cwd],
    queryFn: () => api<Listing>("/move/folders", { query: { path: cwd ?? undefined } }),
    retry: false,
  });
  const move = useWrite((body: object) => api<MoveResult>("/move", { json: { kind, ...body } }));

  const record = (r: MoveResult, extraSkipped: MoveResult["skipped"] = []) => {
    setSummary((s) => ({
      moved: (s?.moved ?? 0) + r.moved,
      skipped: [...(s?.skipped ?? []), ...r.skipped, ...extraSkipped],
      dest: r.dest,
    }));
    setConflicts(r.conflicts.length ? { dest: r.dest, items: r.conflicts.map((c) => ({ ...c, value: c.suggested })) } : null);
    if (r.moved) onMoved();
  };

  const start = () => move.mutate(
    { ids, dest: cwd, new_folder: newFolder.trim() || undefined, on_conflict: onConflict },
    { onSuccess: (r) => { setSummary(null); record(r); } },
  );
  const retry = () => {
    if (!conflicts) return;
    move.mutate(
      { ids: conflicts.items.map((c) => c.id), dest: conflicts.dest, on_conflict: "ask",
        names: Object.fromEntries(conflicts.items.map((c) => [c.id, c.value])) },
      { onSuccess: (r) => record(r) },
    );
  };
  const skipRest = () => {
    if (!conflicts) return;
    setSummary((s) => s && { ...s, skipped: [...s.skipped, ...conflicts.items.map((c) => ({ id: c.id, reason: "skipped (name taken)" }))] });
    setConflicts(null);
  };

  const noun = kind === "scenes" ? (ids.length === 1 ? "video" : "videos") : (ids.length === 1 ? "image" : "images");
  const dir = listing.data;
  const finished = summary && !conflicts;

  return (
    <Modal title="Move to folder" onClose={onClose} wide>
      {finished ? (
        <div className="space-y-3 text-sm" data-testid="move-summary">
          <p>Moved {summary.moved} {summary.moved === 1 ? "item" : "items"} to <code className="break-all text-xs">{summary.dest}</code>.</p>
          {summary.skipped.length > 0 && (
            <div className="text-amber-300">
              {summary.skipped.length} not moved:
              <ul className="mt-1 list-disc pl-5 text-xs">
                {[...new Set(summary.skipped.map((s) => s.reason))].map((r) => (
                  <li key={r}>{summary.skipped.filter((s) => s.reason === r).length} × {r}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="flex justify-end"><button className="btn-primary" onClick={onClose}>Done</button></div>
        </div>
      ) : conflicts ? (
        <div className="space-y-3" data-testid="move-conflicts">
          <p className="text-sm text-zinc-300">
            {summary && summary.moved > 0 && `Moved ${summary.moved}. `}
            {conflicts.items.length === 1 ? "A file with this name is" : "Files with these names are"} already in that folder.
            Choose new names (the extension stays):
          </p>
          <ul className="max-h-[40vh] space-y-2 overflow-y-auto">
            {conflicts.items.map((c, i) => (
              <li key={c.id} className="flex flex-wrap items-center gap-2 text-sm">
                <span className="w-48 truncate text-zinc-400" title={c.name}>{c.name}</span>
                <span className="text-zinc-600">→</span>
                <input className="input min-w-48 flex-1" aria-label={`New name for ${c.name}`} value={c.value}
                       onChange={(e) => setConflicts({ ...conflicts, items: conflicts.items.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)) })} />
              </li>
            ))}
          </ul>
          {move.isError && <ErrorBox error={move.error} />}
          <div className="flex justify-end gap-2">
            <button className="btn-ghost" onClick={skipRest}>Skip {conflicts.items.length === 1 ? "it" : "these"}</button>
            <button className="btn-primary" disabled={move.isPending} onClick={retry}>{move.isPending ? "Moving…" : "Move with these names"}</button>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex items-center gap-2 text-sm">
            <button className="btn-ghost py-1" disabled={cwd === null}
                    onClick={() => setCwd(dir?.parent ?? null)}>↰ Up</button>
            <code className="truncate text-xs text-zinc-400" data-testid="move-path">
              {dir?.library ? `${dir.library.name}${dir.relative ? ` / ${dir.relative}` : ""}` : "Choose a library"}
            </code>
          </div>
          <div className="max-h-[40vh] overflow-y-auto rounded-md border border-zinc-800 bg-zinc-950 p-2">
            {listing.isPending ? <Spinner className="my-6" /> : listing.isError ? <ErrorBox error={listing.error} /> : (
              <ul>
                {dir!.entries.map((e) => (
                  <li key={e.path}>
                    <button type="button" onClick={() => setCwd(e.path)} aria-label={`Open ${e.name}`}
                            className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-zinc-900">
                      <span aria-hidden className="text-amber-400/80">📁</span>
                      <span className="truncate">{e.name}</span>
                      {e.mine && <span className="chip ml-auto">yours</span>}
                    </button>
                  </li>
                ))}
                {!dir!.entries.length && (
                  <li className="py-6 text-center text-sm text-zinc-500">
                    {cwd === null ? "There's no library you can move files to." : "No folders here."}
                  </li>
                )}
              </ul>
            )}
          </div>
          {cwd !== null && (
            <>
              <label className="block text-sm">
                <span className="label">New folder here (optional; "a/b" makes both)</span>
                <input className="input" value={newFolder} onChange={(e) => setNewFolder(e.target.value)} placeholder="e.g. Performer name" />
              </label>
              <label className="flex flex-wrap items-center gap-2 text-sm text-zinc-300">
                If a file with the same name is already there:
                <select className="input w-auto" aria-label="If the name is taken" value={onConflict}
                        onChange={(e) => setOnConflict(e.target.value as OnConflict)}>
                  <option value="ask">let me choose a name</option>
                  <option value="rename">add a number to the name</option>
                  <option value="skip">leave the file where it is</option>
                </select>
              </label>
            </>
          )}
          {move.isError && <ErrorBox error={move.error} />}
          <div className="flex justify-end gap-2">
            <button className="btn-ghost" onClick={onClose}>Cancel</button>
            <button className="btn-primary" disabled={cwd === null || move.isPending} onClick={start}>
              {move.isPending ? "Moving…" : `Move ${ids.length.toLocaleString()} ${noun} ${newFolder.trim() ? `into “${newFolder.trim()}”` : "here"}`}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
