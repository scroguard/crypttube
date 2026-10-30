import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { usePageSize } from "../lib/pageSize";
import { api, type Query } from "./client";
import type { AuthStatus, Page } from "./types";

export function useAuthStatus() {
  return useQuery({ queryKey: ["auth", "status"], queryFn: () => api<AuthStatus>("/auth/status"), staleTime: 60_000 });
}

export function useMe() {
  const user = useAuthStatus().data?.user ?? null;
  return {
    user,
    isAdmin: user?.role === "admin",
    // Editors manage global content and metadata; uploaders manage their own.
    canEdit: user?.role === "admin" || user?.role === "editor",
    canUpload: !!user?.can_upload,
    canCreate: user?.role === "admin" || user?.role === "editor" || !!user?.can_upload,
  };
}

export interface Paging {
  page: number;              // 0-based
  pageCount: number | null;  // null until the total is known
  hasPrev: boolean;
  hasNext: boolean;
  busy: boolean;
  prev: () => void;
  next: () => void;
  first: () => void;
  last: () => void;
  goTo: (page: number) => void;   // 0-based; clamped to the valid range
  /** 1-based positions of the first and last item shown, and how many there are in all. */
  from: number;
  to: number;
  total: number | null;
}

/**
 * A list from the API. With the "items per page" preference unset it accumulates
 * cursor pages for continuous scrolling. With a page size chosen it shows one page at a
 * time (`paging`), and any page can be opened directly (first, last, by number).
 */
export function useInfiniteList<T>(path: string, query: Query, { limit = 60, enabled = true, withTotal = false } = {}) {
  const pageSize = usePageSize();
  const paged = pageSize !== null;
  const size = pageSize ?? limit;
  const q = useInfiniteQuery({
    queryKey: ["list", path, query, size, withTotal],
    queryFn: ({ pageParam, signal }) =>
      api<Page<T>>(path, {
        // The total is only computed for the first page; later pages don't need it.
        query: { ...query, limit: size, cursor: pageParam, with_total: withTotal && !pageParam ? true : undefined },
        signal,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: enabled && !paged,
  });

  // Current page index, reset whenever the list itself (filters, sort, size) changes.
  const listKey = JSON.stringify([path, query, size]);
  const [pos, setPos] = useState({ key: listKey, page: 0 });
  if (pos.key !== listKey) setPos({ key: listKey, page: 0 });
  const wanted = pos.key === listKey ? pos.page : 0;

  // Paged mode: the first page also carries the total (for the page count); any other
  // page is fetched by its offset, so there's no need to walk there page by page.
  const firstPage = useQuery({
    queryKey: ["list", path, query, size, "page", 0],
    queryFn: ({ signal }) => api<Page<T>>(path, { query: { ...query, limit: size, with_total: true }, signal }),
    enabled: enabled && paged,
  });
  const pagedTotal = firstPage.data?.total ?? null;
  const pageCount = pagedTotal !== null ? Math.max(1, Math.ceil(pagedTotal / size)) : null;
  const page = pageCount !== null ? Math.min(wanted, pageCount - 1) : wanted;
  const laterPage = useQuery({
    queryKey: ["list", path, query, size, "page", page],
    queryFn: ({ signal }) => api<Page<T>>(path, { query: { ...query, limit: size, offset: page * size }, signal }),
    enabled: enabled && paged && page > 0,
    placeholderData: keepPreviousData,
  });

  if (!paged) {
    const pages = q.data?.pages ?? [];
    return { ...q, items: pages.flatMap((p) => p.items), total: pages[0]?.total ?? null, paging: null as Paging | null };
  }
  const active = page > 0 ? laterPage : firstPage;
  const go = (n: number) => {
    const max = pageCount !== null ? pageCount - 1 : n;
    setPos({ key: listKey, page: Math.max(0, Math.min(Math.floor(n) || 0, max)) });
    window.scrollTo({ top: 0 });
  };
  // While the next page loads the previous one stays on screen: describe what is shown.
  const shown = active.isPlaceholderData ? 0 : active.data?.items.length ?? 0;
  const paging: Paging = {
    page,
    pageCount,
    hasPrev: page > 0,
    hasNext: pageCount !== null ? page < pageCount - 1 : !!active.data?.next_cursor,
    busy: active.isFetching,
    prev: () => go(page - 1),
    next: () => go(page + 1),
    first: () => go(0),
    last: () => pageCount !== null && go(pageCount - 1),
    goTo: go,
    from: shown ? page * size + 1 : 0,
    to: page * size + shown,
    total: pagedTotal,
  };
  return {
    ...q,
    isPending: active.isPending, isError: active.isError, error: active.error,
    hasNextPage: false, isFetchingNextPage: false,
    items: active.data?.items ?? [], total: pagedTotal, paging: paging as Paging | null,
  };
}

export function useDetail<T>(path: string | null) {
  return useQuery({
    queryKey: ["detail", path],
    queryFn: ({ signal }) => api<T>(path!, { signal }),
    enabled: path !== null,
  });
}

/** A mutation that refreshes all cached data afterwards (lists, details, counts). */
export function useWrite<TArgs, TResult = unknown>(fn: (args: TArgs) => Promise<TResult>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => qc.invalidateQueries({ predicate: (q) => q.queryKey[0] !== "auth" }),
  });
}

/** Save a rating. `error` belongs to `path`: it doesn't follow the user to another item. */
export function useRate(path: string) {
  const write = useWrite(({ to, rating }: { to: string; rating: number | null }) =>
    api(to, { method: "PATCH", json: { rating } }));
  return {
    save: (rating: number | null) => write.mutate({ to: path, rating }),
    error: write.variables?.to === path ? write.error : null,
  };
}

/** Sign out (this device or everywhere), then drop all cached data and show the login screen. */
export function useLogout() {
  const qc = useQueryClient();
  return async (everywhere = false) => {
    await api("/auth/logout", { json: { everywhere } });
    // Reset (not clear): the mounted auth-status observer must refetch to see "signed out".
    await qc.resetQueries({ queryKey: ["auth"] });
    qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "auth" });
  };
}

export interface Preferences { keep_selection_across_pages: boolean }

/** The signed-in user's own preferences (account page). */
export function usePreferences() {
  return useQuery({ queryKey: ["preferences"], queryFn: () => api<Preferences>("/me/preferences"), staleTime: 300_000 });
}
