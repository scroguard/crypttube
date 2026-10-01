import { useState } from "react";
import type { Query } from "../api/client";
import { useInfiniteList } from "../api/hooks";
import type { ImageItem } from "../api/types";
import { ImageTile } from "./Cards";
import InfiniteGrid from "./InfiniteGrid";
import Lightbox from "./Lightbox";

/** Thumbnail grid + lightbox over any image list endpoint. */
export default function ImageBrowser({ path, query = {} }: { path: string; query?: Query }) {
  const list = useInfiniteList<ImageItem>(path, query, { limit: 120, withTotal: true });
  const [open, setOpen] = useState<number | null>(null);
  return (
    <>
      <InfiniteGrid list={list} layout="square" empty="No images."
                    render={(img, i) => <ImageTile image={img} onOpen={() => setOpen(i)} />} />
      {/* In paged mode the lightbox steps through the current page only. */}
      {open !== null && (
        <Lightbox images={list.items} index={open} onIndex={setOpen} onClose={() => setOpen(null)}
                  total={list.paging ? list.items.length : list.total}
                  onNearEnd={() => !list.paging && list.hasNextPage && !list.isFetchingNextPage && list.fetchNextPage()} />
      )}
    </>
  );
}
