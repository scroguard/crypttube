import { useSearchParams } from "react-router";
import { useInfiniteList, useMe } from "../api/hooks";
import type { StudioItem } from "../api/types";
import { StudioCard } from "../components/Cards";
import { CreateEntityButton } from "../components/EntityDialogs";
import { SearchBox, Toolbar } from "../components/FilterBar";
import InfiniteGrid from "../components/InfiniteGrid";
import { plural } from "../lib/format";

export default function Studios() {
  const [p, setP] = useSearchParams();
  const { canEdit } = useMe();
  const q = p.get("q") ?? "";
  const roots = p.get("roots") === "1";
  const list = useInfiniteList<StudioItem>("/studios", { q: q || undefined, root_only: roots || undefined }, { withTotal: true });
  return (
    <div>
      <Toolbar>
        <SearchBox value={q} onChange={(v) => setP(v ? { q: v } : {}, { replace: true })} placeholder="Search studios…" />
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" className="accent-accent" checked={roots} onChange={(e) => setP(e.target.checked ? { roots: "1" } : {})} /> Top-level only
        </label>
        <span className="ml-auto text-sm text-zinc-500">{list.total != null && plural(list.total, "studio")}</span>
        {canEdit && <CreateEntityButton kind="studios" label="studio" />}
      </Toolbar>
      <InfiniteGrid list={list} layout="portrait" render={(s) => <StudioCard studio={s} />} empty="No studios yet." />
    </div>
  );
}
