import { useEffect, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router";
import { readFilters, writeFilters, type FilterSpec, type FilterValues } from "../lib/filters";

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

export function Toolbar({ children }: { children: ReactNode }) {
  return <div className="mb-4 flex flex-wrap items-center gap-2">{children}</div>;
}
