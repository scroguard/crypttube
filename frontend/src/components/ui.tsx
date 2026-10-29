import { useEffect, useRef, type ReactNode } from "react";
import { ApiError } from "../api/client";

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <div className={`flex justify-center ${className}`} role="status" aria-label="Loading">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-zinc-700 border-t-accent" />
    </div>
  );
}

export function ErrorBox({ error, className = "" }: { error: unknown; className?: string }) {
  const msg = error instanceof ApiError ? error.message : error instanceof Error ? error.message : String(error);
  return (
    <div className={`rounded-md border border-red-900 bg-red-950/50 px-4 py-3 text-sm text-red-200 ${className}`} role="alert">
      {msg}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-16 text-center text-sm text-zinc-500">{children}</p>;
}

export function Modal({ title, onClose, children, wide = false }: {
  title: string; onClose: () => void; children: ReactNode; wide?: boolean;
}) {
  const body = useRef<HTMLDivElement>(null);
  // Callers pass inline closures (a new function every render). Reading the latest
  // one through a ref keeps the effects below from re-running on each keystroke,
  // which used to yank focus out of the field being typed in.
  const close = useRef(onClose);
  close.current = onClose;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close.current();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Initial focus, once: the first form field in the body (never the header's close button).
  useEffect(() => {
    const el = body.current;
    (el?.querySelector<HTMLElement>("input:not([type=hidden]), textarea, select")
      ?? el?.querySelector<HTMLElement>("button"))?.focus();
  }, []);

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 pt-[10vh]"
         onMouseDown={(e) => e.target === e.currentTarget && close.current()}>
      <div role="dialog" aria-label={title}
           className={`card w-full ${wide ? "max-w-3xl" : "max-w-md"} bg-zinc-900 p-5 shadow-2xl`}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button className="text-zinc-400 hover:text-white" onClick={() => close.current()} aria-label="Close">✕</button>
        </div>
        <div ref={body}>{children}</div>
      </div>
    </div>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
    </label>
  );
}

/** 0-100 rating shown as five stars; clickable when onChange is given. */
export function Rating({ value, onChange }: { value: number | null; onChange?: (v: number | null) => void }) {
  const stars = value == null ? 0 : Math.round(value / 20);
  return (
    <span className="inline-flex" aria-label={`Rating ${stars} of 5`}>
      {[1, 2, 3, 4, 5].map((n) => (
        <button key={n} type="button" disabled={!onChange}
                onClick={() => onChange?.(stars === n ? null : n * 20)}
                className={`px-0.5 text-lg leading-none ${n <= stars ? "text-amber-400" : "text-zinc-600"} ${onChange ? "hover:text-amber-300" : "cursor-default"}`}>
          ★
        </button>
      ))}
    </span>
  );
}

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-2 text-sm">
      <input type="checkbox" className="h-4 w-4 accent-accent" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

export function Initials({ name, className = "" }: { name: string; className?: string }) {
  const initials = name.split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
  return (
    <div className={`flex items-center justify-center bg-zinc-800 font-semibold text-zinc-500 ${className}`}>{initials}</div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: Record<T, string>; value: T; onChange: (t: T) => void }) {
  return (
    <div className="mb-4 flex gap-1 border-b border-zinc-800" role="tablist">
      {(Object.entries(tabs) as [T, string][]).map(([k, label]) => (
        <button key={k} role="tab" aria-selected={value === k} onClick={() => onChange(k)}
                className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium ${value === k ? "border-accent text-white" : "border-transparent text-zinc-400 hover:text-white"}`}>
          {label}
        </button>
      ))}
    </div>
  );
}
