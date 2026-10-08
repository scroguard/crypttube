import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router";
import { readFilters, writeFilters, type FilterSpec, type FilterValues } from "../lib/filters";
import { PAGE_SIZES, parsePageSize, setPageSize, usePageSize } from "../lib/pageSize";

/** Filters live in the URL, so they survive reloads, are shareable, and Back works. */
export function useUrlFilters(spec: FilterSpec, defaults: FilterValues = {}) {
  const [params, setParams] = useSearchParams();
  const values = { ...defaults, ...readFilters(params, spec) };
  function update(patch: FilterValues, replace = false) {
    setParams(writeFilters({ ...readFilters(params, spec), ...patch }, spec), { replace });
  }
  return [values, update, () => setParams(new URLSearchParams())] as const;
}

export function SearchBox({ value, onChange, placeholder = "Search…" }: {
  value: string; onChange: (v: string) => void; placeholder?: string;
}) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  useEffect(() => {
    if (text === value) return;
    const t = setTimeout(() => onChange(text), 300);
    return () => clearTimeout(t);
  }, [text]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <input type="search" className="input max-w-xs" placeholder={placeholder} value={text}
           onChange={(e) => setText(e.target.value)} aria-label="Search" />
  );
}

export function SortControl({ sorts, sort, desc, onChange }: {
  sorts: Record<string, string>; sort: string; desc: boolean;
  onChange: (sort: string, desc: boolean) => void;
}) {
  return (
    <div className="flex">
      <select className="input w-auto rounded-r-none" value={sort} onChange={(e) => onChange(e.target.value, desc)} aria-label="Sort by">
        {Object.entries(sorts).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
      </select>
      <button className="btn-ghost rounded-l-none border border-l-0 border-zinc-700" onClick={() => onChange(sort, !desc)}
              aria-label={desc ? "Descending" : "Ascending"} title={desc ? "Descending" : "Ascending"}>
        {desc ? "↓" : "↑"}
      </button>
    </div>
  );
}

/**
 * The search/sort/filter/select bar of a list page. It stays on screen under the site
 * header while the list scrolls, and publishes its height as --toolbar-h so other bars
 * (an image list's selection buttons) can stick just below it.
 */
export function Toolbar({ children }: { children: ReactNode }) {
  const bar = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = bar.current;
    if (!el) return;
    const root = document.documentElement;
    const publish = () => root.style.setProperty("--toolbar-h", `${el.offsetHeight}px`);
    publish();
    const observer = new ResizeObserver(publish);
    observer.observe(el);
    return () => { observer.disconnect(); root.style.removeProperty("--toolbar-h"); };
  }, []);
  return (
    <div ref={bar} data-testid="toolbar"
         className="sticky top-14 z-30 -mx-4 mb-4 flex flex-wrap items-center gap-2 bg-zinc-950/90 px-4 py-2 backdrop-blur">
      {children}
    </div>
  );
}

/** "Items per page" preference (stored in this browser); Scroll = continuous loading. */
export function PageSizeSelect() {
  const size = usePageSize();
  return (
    <select className="input w-auto" value={size ?? ""} aria-label="Items per page" title="Items per page"
            onChange={(e) => setPageSize(parsePageSize(e.target.value))}>
      <option value="">Scroll</option>
      {PAGE_SIZES.map((n) => <option key={n} value={n}>{n} per page</option>)}
    </select>
  );
}
