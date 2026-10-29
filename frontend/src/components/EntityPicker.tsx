import { useQueries, useQuery } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { api } from "../api/client";
import type { Page, Ref } from "../api/types";

export type EntityKind = "tags" | "performers" | "studios";

const LIST_HEIGHT = 256; // max-h-64

/** Resolve display names for ids (shares the cache with detail pages). */
export function useNames(kind: EntityKind, ids: number[]): Map<number, string> {
  const results = useQueries({
    queries: ids.map((id) => ({
      queryKey: ["detail", `/${kind}/${id}`],
      queryFn: () => api<Ref>(`/${kind}/${id}`),
      staleTime: 300_000,
    })),
  });
  const names = new Map<number, string>();
  results.forEach((r, i) => names.set(ids[i], r.data?.name ?? `#${ids[i]}`));
  return names;
}

/**
 * Multi (or single) select with type-ahead search against the list API. Editors can
 * create a missing tag/performer/studio inline.
 */
export default function EntityPicker({ kind, value, onChange, single = false, placeholder, onCreate, linked = false, knownNames }: {
  kind: EntityKind;
  value: number[];
  onChange: (ids: number[]) => void;
  single?: boolean;
  placeholder?: string;
  onCreate?: (name: string) => Promise<number>;
  /** Make the selected names links to their pages (for pickers shown inline on a page). */
  linked?: boolean;
  /** Names the caller already has, for ids whose pages can't be fetched (e.g. filtered out). */
  knownNames?: Map<number, string>;
}) {
  const [text, setText] = useState("");
  const [debounced, setDebounced] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const picked = useRef(new Map<number, string>());
  const names = useNames(kind, value.filter((id) => !knownNames?.has(id) && !picked.current.has(id)));
  for (const source of [picked.current, knownNames]) source?.forEach((name, id) => names.set(id, name));
  // Open the list upward when there's no room below (e.g. in the bulk bar pinned to the
  // bottom of the window), so suggestions are never off-screen.
  const [dropUp, setDropUp] = useState(false);
  useLayoutEffect(() => {
    if (!open || !root.current) return;
    const r = root.current.getBoundingClientRect();
    const below = window.innerHeight - r.bottom;
    setDropUp(below < LIST_HEIGHT + 16 && r.top > below);
  }, [open]);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(text.trim()), 150);
    return () => clearTimeout(t);
  }, [text]);
  useEffect(() => {
    const close = (e: MouseEvent) => !root.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const search = useQuery({
    queryKey: ["pick", kind, debounced],
    queryFn: ({ signal }) => api<Page<Ref>>(`/${kind}`, { query: { q: debounced || undefined, limit: 12 }, signal }),
    enabled: open,
    staleTime: 10_000,
  });
  const options = (search.data?.items ?? []).filter((o) => !value.includes(o.id));
  const exact = search.data?.items.some((o) => o.name.toLowerCase() === debounced.toLowerCase());
  const canCreate = !!onCreate && debounced.length > 0 && !exact;

  function pick(id: number) {
    const option = search.data?.items.find((o) => o.id === id);
    if (option) picked.current.set(id, option.name);
    onChange(single ? [id] : [...value, id]);
    setText("");
    setActive(0);
    if (single) setOpen(false);
  }
  async function create() {
    if (!onCreate) return;
    pick(await onCreate(debounced));
  }

  function onKey(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setActive((a) => Math.min(a + 1, options.length - (canCreate ? 0 : 1))); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
    else if (e.key === "Enter") {
      e.preventDefault();
      if (options[active]) pick(options[active].id);
      else if (canCreate) create();
    } else if (e.key === "Backspace" && !text && value.length) onChange(value.slice(0, -1));
    else if (e.key === "Escape") setOpen(false);
  }

  return (
    <div ref={root} className="relative">
      <div className="input flex min-h-[38px] flex-wrap items-center gap-1 py-1" onClick={() => setOpen(true)}>
        {value.map((id) => (
          <span key={id} className="chip">
            {linked
              ? <Link to={`/${kind}/${id}`} className="hover:text-accent-hover" onClick={(e) => e.stopPropagation()}>{names.get(id)}</Link>
              : names.get(id)}
            <button type="button" aria-label={`Remove ${names.get(id)}`} className="text-zinc-400 hover:text-white"
                    onClick={(e) => { e.stopPropagation(); onChange(value.filter((v) => v !== id)); }}>×</button>
          </span>
        ))}
        <input className="min-w-[6rem] flex-1 bg-transparent py-0.5 text-sm outline-none" value={text}
               placeholder={value.length && single ? "" : placeholder ?? `Add ${kind}…`}
               onChange={(e) => { setText(e.target.value); setOpen(true); setActive(0); }}
               onFocus={() => setOpen(true)} onKeyDown={onKey} aria-label={placeholder ?? `Add ${kind}…`} />
      </div>
      {open && (options.length > 0 || canCreate) && (
        <ul role="listbox" data-drop={dropUp ? "up" : "down"}
            className={`card absolute z-30 max-h-64 w-full overflow-y-auto bg-zinc-900 py-1 shadow-xl ${dropUp ? "bottom-full mb-1" : "mt-1"}`}>
          {options.map((o, i) => (
            <li key={o.id} role="option" aria-selected={i === active}
                className={`cursor-pointer px-3 py-1.5 text-sm ${i === active ? "bg-zinc-800" : ""}`}
                onMouseDown={(e) => { e.preventDefault(); pick(o.id); }} onMouseEnter={() => setActive(i)}>
              {o.name}
            </li>
          ))}
          {canCreate && (
            <li role="option" aria-selected={active === options.length}
                className={`cursor-pointer px-3 py-1.5 text-sm text-accent-hover ${active === options.length ? "bg-zinc-800" : ""}`}
                onMouseDown={(e) => { e.preventDefault(); create(); }}>
              Create “{debounced}”
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

/** onCreate implementation for editors: POST the entity, return its id. */
export function creator(kind: EntityKind) {
  return async (name: string) => (await api<Ref>(`/${kind}`, { json: { name } })).id;
}
