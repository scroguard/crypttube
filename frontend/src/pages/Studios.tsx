import { useSearchParams } from "react-router";
import { useInfiniteList, useMe } from "../api/hooks";
import type { StudioItem } from "../api/types";
import { StudioCard } from "../components/Cards";
import { CreateEntityButton } from "../components/EntityDialogs";
import { SearchBox, Toolbar, PageSizeSelect } from "../components/FilterBar";
import InfiniteGrid from "../components/InfiniteGrid";
import { plural } from "../lib/format";

export default function Studios() {
  const [p, setP] = useSearchParams();
  const { canCreate } = useMe();
  const q = p.get("q") ?? "";
  const roots = p.get("roots") === "1";
  // From the live URL, not this render's params: the search box calls this 300 ms late.
  const set = (k: string, v: string | null) => { const n = new URLSearchParams(window.location.search); if (v) n.set(k, v); else n.delete(k); setP(n, { replace: k === "q" }); };
  const list = useInfiniteList<StudioItem>("/studios", { q: q || undefined, root_only: roots || undefined }, { withTotal: true });
  return (
    <div>
      <Toolbar>
        <SearchBox value={q} onChange={(v) => set("q", v || null)} placeholder="Search studios…" />
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" className="accent-accent" checked={roots} onChange={(e) => set("roots", e.target.checked ? "1" : null)} /> Top-level only
        </label>
        <PageSizeSelect />
        <span className="ml-auto text-sm text-text-subtle">{list.total != null && plural(list.total, "studio")}</span>
        {canCreate && <CreateEntityButton kind="studios" label="studio" />}
      </Toolbar>
      <InfiniteGrid list={list} layout="portrait" render={(s) => <StudioCard studio={s} />} empty="No studios yet." />
    </div>
  );
}
