import { useState } from "react";
import { api } from "../api/client";
import { useWrite } from "../api/hooks";
import EntityPicker, { creator } from "./EntityPicker";
import { ErrorBox } from "./ui";

/** Actions on a selection of scenes, galleries or images (one set-based request each). */
export default function BulkBar({ kind, ids, onDone, onSelectAll }: {
  kind: "scenes" | "galleries" | "images"; ids: number[]; onDone: () => void; onSelectAll: () => void;
}) {
  const [tags, setTags] = useState<number[]>([]);
  const [performers, setPerformers] = useState<number[]>([]);
  const write = useWrite((body: object) => api(`/${kind}/bulk`, { json: { ids, ...body } }));
  const run = (body: object) => write.mutate(body, { onSuccess: () => { setTags([]); setPerformers([]); } });

  return (
    <div className="fixed inset-x-0 bottom-0 z-40 border-t border-zinc-700 bg-zinc-900/95 p-3 backdrop-blur" data-testid="bulk-bar">
      <div className="mx-auto flex max-w-[1920px] flex-wrap items-end gap-3">
        <div className="text-sm">
          <strong>{ids.length}</strong> selected
          <button className="ml-2 text-xs text-zinc-400 underline" onClick={onSelectAll}>select all loaded</button>
          <button className="ml-2 text-xs text-zinc-400 underline" onClick={onDone}>clear</button>
        </div>
        <div className="w-64"><EntityPicker kind="tags" value={tags} onChange={setTags} onCreate={creator("tags")} placeholder="Tags…" /></div>
        <button className="btn-primary" disabled={!tags.length} onClick={() => run({ tags: { mode: "add", ids: tags } })}>Add tags</button>
        <button className="btn-ghost" disabled={!tags.length} onClick={() => run({ tags: { mode: "remove", ids: tags } })}>Remove tags</button>
        <div className="w-56"><EntityPicker kind="performers" value={performers} onChange={setPerformers} onCreate={creator("performers")} placeholder="Performers…" /></div>
        <button className="btn-primary" disabled={!performers.length} onClick={() => run({ performers: { mode: "add", ids: performers } })}>Add performers</button>
        <button className="btn-ghost" disabled={!performers.length} onClick={() => run({ performers: { mode: "remove", ids: performers } })}>Remove performers</button>
        <select className="input w-auto" defaultValue="" onChange={(e) => e.target.value && run({ set: { rating: e.target.value === "none" ? null : +e.target.value } })} aria-label="Set rating">
          <option value="" disabled>Set rating…</option>
          {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n * 20}>{"★".repeat(n)}</option>)}
          <option value="none">No rating</option>
        </select>
        <button className="btn-ghost" onClick={() => run({ set: { organized: true } })}>Mark organized</button>
        {write.isError && <ErrorBox error={write.error} />}
      </div>
    </div>
  );
}
