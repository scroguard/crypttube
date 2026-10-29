import { useInfiniteQuery } from "@tanstack/react-query";
import { type ReactNode } from "react";
import { Link } from "react-router";
import { Virtuoso } from "react-virtuoso";
import { api, type Query } from "../api/client";
import { plural } from "../lib/format";
import { setBrowseView, useBrowseView, type BrowseView } from "../lib/viewPref";
import { Empty, ErrorBox, Initials, Spinner } from "./ui";

interface Group<T> {
  performer: { id: number; name: string; image_url: string | null };
  total: number;
  items: T[];
}
interface GroupsPage<T> {
  groups: Group<T>[];
  next_cursor: string | null;
  unassigned: { total: number; items: T[] } | null;
}
interface Section<T> {
  key: string;
  performer: Group<T>["performer"] | null;   // null = the "Unassigned" section
  total: number;
  items: T[];
}

export function ViewToggle() {
  const view = useBrowseView();
  const btn = (v: BrowseView, label: string) => (
    <button type="button" onClick={() => setBrowseView(v)} aria-pressed={view === v}
            className={`px-3 py-2 text-sm font-medium ${view === v ? "bg-zinc-700 text-white" : "bg-zinc-800 text-zinc-400 hover:text-white"}`}>
      {label}
    </button>
  );
  return (
    <div className="flex overflow-hidden rounded-md" role="group" aria-label="View">
      {btn("grouped", "By performer")}
      {btn("grid", "Grid")}
    </div>
  );
}

/**
 * Items grouped by performer: one section per performer (by name) with their first few
 * items and a link to all of them, then "Unassigned". Sections are virtualized and more
 * performers load as you scroll.
 */
export default function GroupedBrowser<T extends { id: number }>({ kind, query, layout, noun, renderItem, empty }: {
  kind: "scenes" | "images";
  query: Query;
  layout: string;                       // CSS grid classes for a section's items
  noun: string;                         // "scene" / "image"
  renderItem: (item: T, section: T[], index: number) => ReactNode;
  empty: string;
}) {
  const q = useInfiniteQuery({
    queryKey: ["groups", kind, query],
    queryFn: ({ pageParam, signal }) =>
      api<GroupsPage<T>>(`/groups/${kind}`, { query: { ...query, limit: 12, per_group: 12, cursor: pageParam }, signal }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  if (q.isPending) return <Spinner className="mt-16" />;
  if (q.isError) return <ErrorBox error={q.error} />;

  const sections: Section<T>[] = q.data.pages.flatMap((p) =>
    p.groups.map((g) => ({ key: `p${g.performer.id}`, performer: g.performer, total: g.total, items: g.items })));
  const unassigned = q.data.pages[q.data.pages.length - 1].unassigned;
  if (unassigned) sections.push({ key: "unassigned", performer: null, total: unassigned.total, items: unassigned.items });
  if (!sections.length) return <Empty>{empty}</Empty>;

  const seeAll = (s: Section<T>) => (s.performer ? `/${kind}?performers=${s.performer.id}` : `/${kind}?unassigned=1`);

  return (
    <Virtuoso
      useWindowScroll
      data={sections}
      computeItemKey={(_, s) => s.key}
      increaseViewportBy={{ top: 600, bottom: 1200 }}
      endReached={() => q.hasNextPage && !q.isFetchingNextPage && q.fetchNextPage()}
      components={{ Footer: () => (q.isFetchingNextPage ? <Spinner className="py-6" /> : <div className="h-6" />) }}
      itemContent={(_, s) => (
        <section className="pb-8" data-testid="group" aria-label={s.performer?.name ?? "Unassigned"}>
          <header className="mb-3 flex items-center gap-3 border-b border-zinc-800 pb-2">
            {s.performer ? (
              <Link to={`/performers/${s.performer.id}`} className="flex min-w-0 items-center gap-3 hover:text-accent-hover">
                {s.performer.image_url
                  ? <img src={s.performer.image_url} alt="" className="h-9 w-9 rounded-full object-cover" />
                  : <Initials name={s.performer.name} className="h-9 w-9 rounded-full text-xs" />}
                <h2 className="truncate text-lg font-semibold">{s.performer.name}</h2>
              </Link>
            ) : (
              <h2 className="text-lg font-semibold text-zinc-300">Unassigned</h2>
            )}
            <span className="text-sm text-zinc-500" data-testid="group-count">{plural(s.total, noun)}</span>
            {s.total > s.items.length && (
              <Link to={seeAll(s)} className="ml-auto whitespace-nowrap text-sm text-accent-hover hover:underline">
                See all {s.total.toLocaleString()} →
              </Link>
            )}
            {s.total <= s.items.length && (
              <Link to={seeAll(s)} className="ml-auto whitespace-nowrap text-sm text-zinc-500 hover:text-white">Open as grid →</Link>
            )}
          </header>
          <div className={layout}>
            {s.items.map((item, i) => <div key={item.id}>{renderItem(item, s.items, i)}</div>)}
          </div>
        </section>
      )}
    />
  );
}
