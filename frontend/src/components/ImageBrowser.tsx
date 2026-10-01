import { useCallback, useState } from "react";
import type { Query } from "../api/client";
import { useInfiniteList, useMe } from "../api/hooks";
import type { ImageItem } from "../api/types";
import BulkBar from "./BulkBar";
import { ImageTile } from "./Cards";
import InfiniteGrid from "./InfiniteGrid";
import Lightbox from "./Lightbox";

/** Thumbnail grid + lightbox over any image list endpoint, with a select mode for
 * assigning performers/tags to many images at once. */
export default function ImageBrowser({ path, query = {} }: { path: string; query?: Query }) {
  const { canCreate } = useMe();
  const list = useInfiniteList<ImageItem>(path, query, { limit: 120, withTotal: true });
  const [open, setOpen] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<number> | null>(null);
  const toggle = useCallback((id: number) => setSelected((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  }), []);
  return (
    <>
      {canCreate && list.items.length > 0 && (
        <div className="mb-2 flex justify-end">
          <button className={selected ? "btn-primary" : "btn-ghost"} onClick={() => setSelected((s) => (s ? null : new Set()))}>
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
