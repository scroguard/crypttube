import { useEffect, useRef } from "react";

/**
 * Run `onEscape` when Esc is pressed while `active`, unless something more specific owns
 * the key: an open dialog/lightbox handles its own Esc, and Esc inside a text field first
 * just leaves the field (and lets the field close its own suggestion list).
 */
export function useEscape(active: boolean, onEscape: () => void): void {
  const fn = useRef(onEscape);
  fn.current = onEscape;
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (document.querySelector('[role="dialog"]')) return;
      const field = (e.target as HTMLElement).closest?.("input, textarea, select") as HTMLElement | null;
      if (field) {
        field.blur();
        return;
      }
      fn.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active]);
}

/** Take keyboard focus away from whatever had it (e.g. the search box), so the next
 * key press isn't typed into, or swallowed by, a field the user is no longer using. */
export function releaseFocus(): void {
  (document.activeElement as HTMLElement | null)?.blur?.();
}
