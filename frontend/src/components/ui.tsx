import { useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../api/client";

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <div className={`flex justify-center ${className}`} role="status" aria-label="Loading">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-border-strong border-t-accent" />
    </div>
  );
}

export function ErrorBox({ error, className = "" }: { error: unknown; className?: string }) {
  const msg = error instanceof ApiError ? error.message : error instanceof Error ? error.message : String(error);
  return (
    <div className={`rounded-md border border-danger/40 bg-danger-bg px-4 py-3 text-sm text-danger ${className}`} role="alert">
      {msg}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-16 text-center text-sm text-text-subtle">{children}</p>;
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
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-overlay p-4 pt-[10vh]"
         onMouseDown={(e) => e.target === e.currentTarget && close.current()}>
      <div role="dialog" aria-label={title}
           className={`card w-full ${wide ? "max-w-3xl" : "max-w-md"} bg-surface p-5 shadow-2xl`}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button className="text-text-muted hover:text-text" onClick={() => close.current()} aria-label="Close">✕</button>
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

/**
 * 0-100 rating shown as five stars; clickable when onChange is given. A tap shows at once,
 * ahead of the save, and comes back with `error` if the save failed. Stars get finger-sized
 * targets on touch screens.
 */
export function Rating({ value, onChange, error }: {
  value: number | null; onChange?: (v: number | null) => void; error?: Error | null;
}) {
  const [chosen, setChosen] = useState<number | null | undefined>(undefined);
  useEffect(() => setChosen(undefined), [value]);
  useEffect(() => { if (error) setChosen(undefined); }, [error]);
  const shown = chosen === undefined ? value : chosen;
  const stars = shown == null ? 0 : Math.round(shown / 20);
  const choose = (n: number) => {
    const next = stars === n ? null : n * 20;
    setChosen(next);
    onChange?.(next);
  };
  return (
    <span className="inline-flex flex-col items-start">
      <span className="inline-flex" aria-label={`Rating ${stars} of 5`}>
        {[1, 2, 3, 4, 5].map((n) => (
          <button key={n} type="button" disabled={!onChange} onClick={() => choose(n)}
                  aria-label={onChange ? `Rate ${n} of 5` : undefined}
                  className={`touch-manipulation px-0.5 text-lg leading-none pointer-coarse:h-11 pointer-coarse:w-9 pointer-coarse:px-0 pointer-coarse:text-2xl ${n <= stars ? "text-warning" : "text-text-subtle"} ${onChange ? "hover:text-warning" : "cursor-default"}`}>
            ★
          </button>
        ))}
      </span>
      {error && <span role="alert" className="text-xs text-danger">Rating not saved: {error.message}</span>}
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
    <div className={`flex items-center justify-center bg-surface-raised font-semibold text-text-subtle ${className}`}>{initials}</div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: Record<T, string>; value: T; onChange: (t: T) => void }) {
  return (
    <div className="mb-4 flex gap-1 border-b border-border" role="tablist">
      {(Object.entries(tabs) as [T, string][]).map(([k, label]) => (
        <button key={k} role="tab" aria-selected={value === k} onClick={() => onChange(k)}
                className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium ${value === k ? "border-accent text-text" : "border-transparent text-text-muted hover:text-text"}`}>
          {label}
        </button>
      ))}
    </div>
  );
}
