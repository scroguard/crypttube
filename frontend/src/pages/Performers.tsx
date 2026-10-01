import { useSearchParams } from "react-router";
import { useInfiniteList, useMe } from "../api/hooks";
import type { PerformerItem } from "../api/types";
import { PerformerCard } from "../components/Cards";
import { CreateEntityButton } from "../components/EntityDialogs";
import { SearchBox, SortControl, Toolbar } from "../components/FilterBar";
import InfiniteGrid from "../components/InfiniteGrid";
import { GENDERS, plural } from "../lib/format";

const SORTS = { name: "Name", created_at: "Date added", rating: "Rating", birthdate: "Birthdate" };

export default function Performers() {
  const [p, setP] = useSearchParams();
  const { canEdit } = useMe();
  const q = p.get("q") ?? "";
  const sort = p.get("sort") ?? "name";
  const desc = p.get("desc") === "1";
  const gender = p.get("gender") ?? "";
  const favorite = p.get("favorite") === "1";
  const set = (k: string, v: string | null) => { const n = new URLSearchParams(p); if (v) n.set(k, v); else n.delete(k); setP(n, { replace: k === "q" }); };
  const list = useInfiniteList<PerformerItem>("/performers", {
    q: q || undefined, sort, desc, gender: gender || undefined, favorite: favorite || undefined,
  }, { withTotal: true });
  return (
    <div>
      <Toolbar>
        <SearchBox value={q} onChange={(v) => set("q", v)} placeholder="Search names and aliases…" />
        <SortControl sorts={SORTS} sort={sort} desc={desc} onChange={(s, d) => { const n = new URLSearchParams(p); n.set("sort", s); n.set("desc", d ? "1" : "0"); setP(n); }} />
        <select className="input w-auto" value={gender} onChange={(e) => set("gender", e.target.value)} aria-label="Gender">
          <option value="">All genders</option>
          {Object.entries(GENDERS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" className="accent-accent" checked={favorite} onChange={(e) => set("favorite", e.target.checked ? "1" : null)} /> Favorites</label>
        <span className="ml-auto text-sm text-zinc-500">{list.total != null && plural(list.total, "performer")}</span>
        {canEdit && <CreateEntityButton kind="performers" label="performer" />}
      </Toolbar>
      <InfiniteGrid list={list} layout="portrait" render={(x) => <PerformerCard performer={x} />} empty="No performers yet." />
    </div>
  );
}
