import { type ReactNode } from "react";
import { useLocation } from "react-router";
import { VirtuosoGrid, type GridStateSnapshot } from "react-virtuoso";
import { Empty, ErrorBox, Spinner } from "./ui";

// Scroll/render state per history entry, so Back returns to the exact spot.
const snapshots = new Map<string, GridStateSnapshot>();

const LAYOUTS = {
  wide: "grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-x-3 gap-y-5",
  portrait: "grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-3",
  square: "grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-2",
};

interface ListState<T> {
  items: T[];
  isPending: boolean;
  isError: boolean;
  error: unknown;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => unknown;
}

/**
 * Window-scrolled virtualized grid: only rows near the viewport are in the DOM, so a
 * 100k-item library scrolls like a 50-item one. Pages load as the end approaches.
 */
export default function InfiniteGrid<T extends { id: number }>({
  list, render, layout = "wide", empty = "Nothing here yet.",
}: {
  list: ListState<T>;
  render: (item: T, index: number) => ReactNode;
  layout?: keyof typeof LAYOUTS;
  empty?: ReactNode;
}) {
  const location = useLocation();
  if (list.isPending) return <Spinner className="mt-16" />;
  if (list.isError) return <ErrorBox error={list.error} />;
  if (!list.items.length) return <Empty>{empty}</Empty>;
  return (
    <VirtuosoGrid
      useWindowScroll
      data={list.items}
      computeItemKey={(_, item) => item.id}
      listClassName={LAYOUTS[layout]}
      increaseViewportBy={{ top: 400, bottom: 1200 }}
      itemContent={(i, item) => render(item, i)}
      endReached={() => list.hasNextPage && !list.isFetchingNextPage && list.fetchNextPage()}
      restoreStateFrom={snapshots.get(location.key)}
      stateChanged={(s) => snapshots.set(location.key, s)}
      components={{ Footer: () => (list.isFetchingNextPage ? <Spinner className="py-6" /> : <div className="h-6" />) }}
    />
  );
}
