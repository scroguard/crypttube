/** Per-browser preference for the Scenes / Images pages: grouped by performer, or a flat grid. */

import { useSyncExternalStore } from "react";

export type BrowseView = "grouped" | "grid";
const KEY = "browse.view";
const listeners = new Set<() => void>();

function read(): BrowseView {
  try {
    return localStorage.getItem(KEY) === "grid" ? "grid" : "grouped";
  } catch {
    return "grouped";
  }
}

export function setBrowseView(view: BrowseView): void {
  try {
    localStorage.setItem(KEY, view);
  } catch { /* won't persist */ }
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  window.addEventListener("storage", fn);
  return () => {
    listeners.delete(fn);
    window.removeEventListener("storage", fn);
  };
}

export function useBrowseView(): BrowseView {
  return useSyncExternalStore(subscribe, read, () => "grouped" as BrowseView);
}
