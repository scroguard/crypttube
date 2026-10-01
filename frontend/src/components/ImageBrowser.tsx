import { useCallback, useState } from "react";
import type { Query } from "../api/client";
import { useInfiniteList, useMe } from "../api/hooks";
import type { ImageItem } from "../api/types";
import BulkBar, { fetchMatchingIds } from "./BulkBar";
import { ImageTile } from "./Cards";
import InfiniteGrid from "./InfiniteGrid";
import Lightbox from "./Lightbox";

/** Thumbnail grid + lightbox over any image list endpoint, with a select mode for
 * assigning performers/tags to many images at once. `selecting`/`onSelecting` let a
 * parent start in (and observe) select mode. */
export default function ImageBrowser({ path, query = {}, selecting, onSelecting }: {
  path: string; query?: Query; selecting?: boolean; onSelecting?: (on: boolean) => void;
}) {
  const { canCreate } = useMe();
  const list = useInfiniteList<ImageItem>(path, query, { limit: 120, withTotal: true });
  const [open, setOpen] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<number> | null>(selecting ? new Set() : null);
  const [note, setNote] = useState<string | null>(null);
  const toggle = useCallback((id: number) => setSelected((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  }), []);
  function setMode(on: boolean) {
    setSelected(on ? new Set() : null);
    setNote(null);
    onSelecting?.(on);
  }
  // "/images" takes the filters as they are; a gallery's list is selected by gallery id.
  const idsQuery: Query = path === "/images" ? query : { gallery_id: Number(path.split("/")[2]) };
  async function selectAllMatching() {
    const r = await fetchMatchingIds("images", idsQuery);
    setSelected(new Set(r.ids));
    setNote(r.truncated ? "Selected the first 20,000 matches." : r.ids.length ? null : "Nothing here that you can edit.");
  }
  return (
    <>
      {canCreate && (list.items.length > 0 || selected) && (
        <div className="mb-2 flex items-center justify-end gap-2">
          {note && <span className="text-xs text-amber-400">{note}</span>}
          {selected && (
            <button className="btn-ghost" onClick={selectAllMatching} title="Select every image in this list, including ones not loaded yet">
              Select all matching
            </button>
          )}
          <button className={selected ? "btn-primary" : "btn-ghost"} onClick={() => setMode(!selected)}>
            {selected ? "Done selecting" : "Select images"}
          </button>
        </div>
      )}
      <InfiniteGrid list={list} layout="square" empty="No images."
                    render={(img, i) => (
                      <ImageTile image={img} onOpen={() => setOpen(i)}
                                 selected={selected?.has(img.id)} onToggle={selected ? toggle : undefined} />
                    )} />
      {selected && selected.size > 0 && (
        <BulkBar kind="images" ids={[...selected]} onDone={() => setSelected(new Set())}
                 onSelectAll={() => setSelected(new Set(list.items.map((i) => i.id)))} />
      )}
      {/* In paged mode the lightbox steps through the current page only. */}
      {open !== null && (
        <Lightbox images={list.items} index={open} onIndex={setOpen} onClose={() => setOpen(null)}
                  total={list.paging ? list.items.length : list.total}
                  onNearEnd={() => !list.paging && list.hasNextPage && !list.isFetchingNextPage && list.fetchNextPage()} />
      )}
    </>
  );
}
