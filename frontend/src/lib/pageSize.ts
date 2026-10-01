/** Per-browser preference: how lists are paged. null = continuous (infinite) scrolling. */

import { useSyncExternalStore } from "react";

export const PAGE_SIZES = [10, 20, 30, 50, 100, 200] as const;
const KEY = "list.pageSize";
const listeners = new Set<() => void>();

export function parsePageSize(raw: string | null): number | null {
  const n = Number(raw);
  return (PAGE_SIZES as readonly number[]).includes(n) ? n : null;
}

function read(): number | null {
  try {
    return parsePageSize(localStorage.getItem(KEY));
  } catch {
    return null; // storage unavailable (private mode etc.)
  }
}

export function setPageSize(size: number | null): void {
  try {
    if (size === null) localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, String(size));
  } catch { /* the choice just won't persist */ }
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  window.addEventListener("storage", fn); // other tabs
  return () => {
    listeners.delete(fn);
    window.removeEventListener("storage", fn);
  };
}

export function usePageSize(): number | null {
  return useSyncExternalStore(subscribe, read, () => null);
}
