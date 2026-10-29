import { useInfiniteList } from "../api/hooks";
import type { GalleryItem } from "../api/types";
import { GalleryCard } from "../components/Cards";
import EntityPicker from "../components/EntityPicker";
import { SearchBox, SortControl, Toolbar, useUrlFilters, PageSizeSelect } from "../components/FilterBar";
import InfiniteGrid from "../components/InfiniteGrid";
import { plural } from "../lib/format";
import { GALLERY_FILTERS, toQuery } from "../lib/filters";

const SORTS = { created_at: "Date added", release_date: "Release date", title: "Title", rating: "Rating", image_count: "Image count" };

export default function Galleries() {
  const [f, update] = useUrlFilters(GALLERY_FILTERS, { sort: "created_at", desc: true });
  const list = useInfiniteList<GalleryItem>("/galleries", toQuery(f, GALLERY_FILTERS), { withTotal: true });
  return (
    <div>
      <Toolbar>
        <SearchBox value={(f.q as string) ?? ""} onChange={(q) => update({ q }, true)} placeholder="Search titles, folders, performers…" />
        <SortControl sorts={SORTS} sort={f.sort as string} desc={f.desc as boolean} onChange={(sort, desc) => update({ sort, desc })} />
        <div className="w-64"><EntityPicker kind="tags" value={(f.tags as number[]) ?? []} onChange={(tags) => update({ tags })} placeholder="Filter by tag…" /></div>
        <PageSizeSelect />
        <span className="ml-auto text-sm text-text-subtle">{list.total != null && plural(list.total, "gallery", "galleries")}</span>
      </Toolbar>
      <InfiniteGrid list={list} render={(g) => <GalleryCard gallery={g} />} empty="No galleries." />
    </div>
  );
}
