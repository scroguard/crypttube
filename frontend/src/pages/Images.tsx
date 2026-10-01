import { useSearchParams } from "react-router";
import EntityPicker from "../components/EntityPicker";
import ImageBrowser from "../components/ImageBrowser";
import { SortControl, Toolbar } from "../components/FilterBar";

const SORTS = { created_at: "Date added", title: "Title", rating: "Rating" };

export default function Images() {
  const [params, setParams] = useSearchParams();
  const sort = params.get("sort") ?? "created_at";
  const desc = params.get("desc") !== "0";
  const tags = params.getAll("tags").map(Number);
  function set(next: { sort?: string; desc?: boolean; tags?: number[] }) {
    const p = new URLSearchParams();
    p.set("sort", next.sort ?? sort);
    p.set("desc", (next.desc ?? desc) ? "1" : "0");
    (next.tags ?? tags).forEach((t) => p.append("tags", String(t)));
    setParams(p);
  }
  return (
    <div>
      <Toolbar>
        <SortControl sorts={SORTS} sort={sort} desc={desc} onChange={(s, d) => set({ sort: s, desc: d })} />
        <div className="w-64"><EntityPicker kind="tags" value={tags} onChange={(t) => set({ tags: t })} placeholder="Filter by tag…" /></div>
      </Toolbar>
      <ImageBrowser path="/images" query={{ sort, desc, tag_ids: tags }} />
    </div>
  );
}
