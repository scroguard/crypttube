import { useState } from "react";
import type { ImageItem } from "../api/types";
import { ImageTile } from "../components/Cards";
import EntityPicker from "../components/EntityPicker";
import { PageSizeSelect, SearchBox, SortControl, Toolbar, useUrlFilters } from "../components/FilterBar";
import GroupedBrowser, { ViewToggle } from "../components/GroupedBrowser";
import ImageBrowser from "../components/ImageBrowser";
import { LAYOUTS } from "../components/InfiniteGrid";
import Lightbox from "../components/Lightbox";
import { IMAGE_FILTERS, toQuery } from "../lib/filters";
import { useBrowseView } from "../lib/viewPref";

const SORTS = { created_at: "Date added", title: "Title", rating: "Rating" };

export default function Images() {
  const [f, update, reset] = useUrlFilters(IMAGE_FILTERS, { sort: "created_at", desc: true });
  const view = useBrowseView();
  const [box, setBox] = useState<{ items: ImageItem[]; index: number } | null>(null);
  const query = toQuery(f, IMAGE_FILTERS);
  const narrowed = !!f.performers || f.unassigned !== undefined;
  const grouped = view === "grouped" && !narrowed;

  return (
    <div>
      <Toolbar>
        <SearchBox value={(f.q as string) ?? ""} onChange={(q) => update({ q }, true)} placeholder="Search images…" />
        <SortControl sorts={SORTS} sort={f.sort as string} desc={f.desc as boolean} onChange={(sort, desc) => update({ sort, desc })} />
        <ViewToggle />
        <div className="w-56"><EntityPicker kind="tags" value={(f.tags as number[]) ?? []} onChange={(tags) => update({ tags })} placeholder="Filter by tag…" /></div>
        <div className="w-56"><EntityPicker kind="performers" value={(f.performers as number[]) ?? []} onChange={(performers) => update({ performers })} placeholder="Filter by performer…" /></div>
        {f.unassigned === true && <span className="chip">No performer</span>}
        {narrowed && <button className="btn-ghost" onClick={reset}>Clear</button>}
        {!grouped && <PageSizeSelect />}
      </Toolbar>
      {grouped ? (
        <GroupedBrowser<ImageItem> kind="images" query={query} layout={LAYOUTS.square} noun="image" empty="No images."
                                   renderItem={(img, section, i) => (
                                     <ImageTile image={img} onOpen={() => setBox({ items: section, index: i })} />
                                   )} />
      ) : (
        <ImageBrowser path="/images" query={query} />
      )}
      {box && (
        <Lightbox images={box.items} index={box.index} onIndex={(index) => setBox({ ...box, index })} onClose={() => setBox(null)} />
      )}
    </div>
  );
}
