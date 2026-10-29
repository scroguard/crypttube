import { Link } from "react-router";
import type { Ref } from "../api/types";

export default function Chips({ items, to }: { items: Ref[]; to: string }) {
  if (!items.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((t) => <Link key={t.id} to={`/${to}/${t.id}`} className="chip">{t.name}</Link>)}
    </div>
  );
}
