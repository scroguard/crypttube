import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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

/** Cursor-paginated list, flattened for a virtualized grid. */
export function useInfiniteList<T>(path: string, query: Query, { limit = 60, enabled = true, withTotal = false } = {}) {
  const q = useInfiniteQuery({
    queryKey: ["list", path, query, limit, withTotal],
    queryFn: ({ pageParam, signal }) =>
      api<Page<T>>(path, {
        // The total is only computed for the first page; later pages don't need it.
        query: { ...query, limit, cursor: pageParam, with_total: withTotal && !pageParam ? true : undefined },
        signal,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled,
  });
  const items = q.data?.pages.flatMap((p) => p.items) ?? [];
  return { ...q, items, total: q.data?.pages[0]?.total ?? null };
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
