import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { ErrorBox, Modal, Spinner } from "./ui";

export interface BrowseResult {
  path: string;
  root: string;
  parent: string | null;
  library: { id: number; path: string } | null;
  entries: { name: string; path: string; library_id: number | null }[];
  truncated: boolean;
}

/**
 * Server-side folder browser (admin). Navigation can't go above `floor` (defaults to the
 * media root). `blocked` may return a reason why the current folder can't be chosen.
 */
export default function DirectoryPicker({ title, start, floor, selectLabel, onSelect, onClose, blocked }: {
  title: string;
  start?: string;
  floor?: string;
  selectLabel: string;
  onSelect: (path: string) => void;
  onClose: () => void;
  blocked?: (dir: BrowseResult) => string | null;
}) {
  const [cwd, setCwd] = useState<string | undefined>(start);
  const q = useQuery({
    queryKey: ["browse", cwd],
    queryFn: () => api<BrowseResult>("/libraries/browse", { query: { path: cwd } }),
    retry: false,
  });
  const dir = q.data;
  const base = floor ?? dir?.root ?? "";
  const reason = dir && blocked ? blocked(dir) : null;

  // Breadcrumbs from the floor down to the current folder.
  const crumbs: { label: string; path: string }[] = [];
  if (dir) {
    crumbs.push({ label: base.split("/").filter(Boolean).pop() ?? "/", path: base });
    const rest = dir.path.slice(base.length).split("/").filter(Boolean);
    rest.forEach((part, i) => crumbs.push({ label: part, path: `${base}/${rest.slice(0, i + 1).join("/")}` }));
  }
  const canGoUp = dir?.parent != null && dir.path !== base;

  return (
    <Modal title={title} onClose={onClose} wide>
      <div className="mb-3 flex flex-wrap items-center gap-1 text-sm" aria-label="Current folder">
        {crumbs.map((c, i) => (
          <span key={c.path} className="flex items-center gap-1">
            {i > 0 && <span className="text-zinc-600">/</span>}
            <button type="button" onClick={() => setCwd(c.path)}
                    className={`rounded px-1.5 py-0.5 font-mono hover:bg-zinc-800 ${i === crumbs.length - 1 ? "text-white" : "text-zinc-400"}`}>
              {c.label}
            </button>
          </span>
        ))}
      </div>

      <div className="h-80 overflow-y-auto rounded-md border border-zinc-800 bg-zinc-950" data-testid="folder-list">
        {q.isPending ? <Spinner className="mt-24" /> : q.isError ? (
          <div className="m-3 space-y-3">
            <ErrorBox error={q.error} />
            <button type="button" className="btn-ghost" onClick={() => setCwd(floor)}>Go to {floor ?? "the media root"}</button>
          </div>
        ) : (
          <ul className="divide-y divide-zinc-900">
            {canGoUp && (
              <li>
                <button type="button" onClick={() => setCwd(dir!.parent!)} className="w-full px-3 py-2 text-left text-sm text-zinc-400 hover:bg-zinc-900">
                  ↰ Up one level
                </button>
              </li>
            )}
            {dir!.entries.map((e) => (
              <li key={e.path}>
                <button type="button" onClick={() => setCwd(e.path)} aria-label={`Open ${e.name}`}
                        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-zinc-900">
                  <span aria-hidden className="text-amber-400/80">📁</span>
                  <span className="truncate">{e.name}</span>
                  {e.library_id && <span className="chip ml-auto shrink-0">library</span>}
                </button>
              </li>
            ))}
            {!dir!.entries.length && <li className="px-3 py-6 text-center text-sm text-zinc-500">No subfolders</li>}
            {dir!.truncated && <li className="px-3 py-2 text-xs text-zinc-500">Showing the first 5,000 folders.</li>}
          </ul>
        )}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <code className="mr-auto min-w-0 truncate text-xs text-zinc-400" data-testid="picker-path">{dir?.path}</code>
        {reason && <span className="w-full text-xs text-amber-400">{reason}</span>}
        <button type="button" className="btn-ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="btn-primary" disabled={!dir || !!reason}
                onClick={() => dir && onSelect(dir.path)}>
          {selectLabel}
        </button>
      </div>
    </Modal>
  );
}
