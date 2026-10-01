import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  pageCount: number | null;  // known when the list was asked for its total
  hasPrev: boolean;
  hasNext: boolean;
  busy: boolean;
  prev: () => void;
  next: () => void;
}

/**
 * Cursor-paginated list. With the "items per page" preference unset it accumulates
 * pages for continuous scrolling; with a page size chosen it exposes one page at a time
 * (`paging`), fetching the next cursor page on demand and keeping visited pages cached.
 */
export function useInfiniteList<T>(path: string, query: Query, { limit = 60, enabled = true, withTotal = false } = {}) {
  const pageSize = usePageSize();
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
    enabled,
  });
  const pages = q.data?.pages ?? [];
  const total = pages[0]?.total ?? null;

  // Current page index, reset whenever the list itself (filters, sort, size) changes.
  const listKey = JSON.stringify([path, query, size]);
  const [pos, setPos] = useState({ key: listKey, page: 0 });
  const page = pos.key === listKey ? Math.min(pos.page, Math.max(pages.length - 1, 0)) : 0;
  if (pos.key !== listKey) setPos({ key: listKey, page: 0 });

  const go = (n: number) => {
    setPos({ key: listKey, page: n });
    window.scrollTo({ top: 0 });
  };
  const paging: Paging | null = pageSize === null ? null : {
    page,
    pageCount: total !== null ? Math.max(1, Math.ceil(total / size)) : null,
    hasPrev: page > 0,
    hasNext: page < pages.length - 1 || q.hasNextPage,
    busy: q.isFetchingNextPage,
    prev: () => go(page - 1),
    next: async () => {
      if (page >= pages.length - 1) await q.fetchNextPage();
      go(page + 1);
    },
  };
  const items = paging ? pages[page]?.items ?? [] : pages.flatMap((p) => p.items);
  return { ...q, items, total, paging };
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
