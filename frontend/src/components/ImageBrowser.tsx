import { useCallback, useState, useEffect } from "react";
import type { Query } from "../api/client";
import { useInfiniteList, useMe, usePreferences } from "../api/hooks";
import type { ImageItem } from "../api/types";
import { useMarquee } from "../lib/useMarquee";
import { SearchBox } from "./FilterBar";
import BulkBar, { fetchMatchingIds } from "./BulkBar";
import { matchingNote, skippedNote } from "../lib/selection";
import { ImageTile } from "./Cards";
import InfiniteGrid from "./InfiniteGrid";
import Lightbox from "./Lightbox";
import { releaseFocus, useEscape } from "../lib/useEscape";

/** Thumbnail grid + lightbox over any image list endpoint, with a select mode for
 * assigning performers/tags to many images at once. `selecting`/`onSelecting` let a
 * parent start in (and observe) select mode. */
export default function ImageBrowser({ path, query: given = {}, selecting, onSelecting, scope }: {
  path: string; query?: Query; selecting?: boolean; onSelecting?: (on: boolean) => void;
  /** Show a search box limited to this list, e.g. "this studio" on a studio's page. */
  scope?: string;
}) {
  const [q, setQ] = useState("");
  const query = scope && q ? { ...given, q } : given;
  const { canCreate } = useMe();
  const list = useInfiniteList<ImageItem>(path, query, { limit: 120, withTotal: true });
  const [open, setOpen] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<number> | null>(selecting ? new Set() : null);
  // Belongs to the selection it describes: any change to that makes a new Set, and hides it.
  const [matched, setNote] = useState<{ text: string; selection: Set<number> } | null>(null);
  const note = matched && matched.selection === selected ? matched.text : undefined;
  // Follow the parent's select mode without remounting (which would lose the current page).
  useEffect(() => {
    if (selecting === undefined) return;
    setSelected((s) => (selecting ? s ?? new Set() : null));
  }, [selecting]);
  // Click and drag to select a whole area at once.
  const marquee = useMarquee(!!selected, (ids) => setSelected((s) => new Set([...(s ?? []), ...ids])));
  const toggle = useCallback((id: number) => setSelected((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  }), []);
  function setMode(on: boolean) {
    releaseFocus();
    setSelected(on ? new Set() : null);
    setNote(null);
    onSelecting?.(on);
  }
  useEscape(!!selected, () => setMode(false));   // Esc leaves select mode
  // "/images" takes the filters as they are; a gallery's list is selected by gallery id.
  const idsQuery: Query = path === "/images" ? query : { gallery_id: Number(path.split("/")[2]) };
  // Adds to what is already selected, so pages can be gone through one by one.
  const selectShown = () => {
    const editable = list.items.filter((i) => i.can_edit);
    const selection = new Set([...(selected ?? []), ...editable.map((i) => i.id)]);
    const skipped = list.items.length - editable.length;
    setSelected(selection);
    setNote(skipped ? { text: skippedNote(skipped), selection } : null);
  };
  // A paged list starts each page with nothing selected, unless the user chose to keep
  // the selection across pages (account page).
  const keepAcrossPages = usePreferences().data?.keep_selection_across_pages ?? false;
  const shownPage = list.paging?.page;
  useEffect(() => {
    if (keepAcrossPages) return;
    setSelected((s) => (s && s.size ? new Set() : s));
    setNote(null);
  }, [shownPage]); // eslint-disable-line react-hooks/exhaustive-deps
  const deselectAll = () => { setSelected((s) => (s ? new Set() : s)); setNote(null); };
  async function selectAllMatching() {
    const before = selected?.size ?? 0;
    const r = await fetchMatchingIds("images", idsQuery);
    const selection = new Set(r.ids);
    setSelected(selection);
    setNote({ text: matchingNote(r, before, list.items.map((i) => i.id)), selection });
  }
  return (
    <>
      {marquee.overlay}
      {(scope || (canCreate && (list.items.length > 0 || selected))) && (
        // Sticks under the page's toolbar (or the site header where there is none), so
        // "Done selecting" is always at hand.
        <div className="sticky z-20 -mx-4 mb-2 flex flex-wrap items-center justify-end gap-2 bg-bg/90 px-4 py-1.5 backdrop-blur"
             style={{ top: "calc(3.5rem + var(--toolbar-h, 0px))" }} data-testid="select-top">
          {scope && <span className="mr-auto"><SearchBox value={q} onChange={setQ} placeholder={`Search in ${scope}…`} /></span>}
          {note && !selected?.size && <span className="text-xs text-warning">{note}</span>}
          {selected && (
          <>
            <button className="btn-ghost" onClick={selectShown}
                    title={`Add every image ${list.paging ? "on this page" : "shown so far"} to the selection`}>
              {list.paging ? "Select this page" : "Select all shown"}
            </button>
            <button className="btn-ghost" onClick={selectAllMatching} title="Select every image in this list, including ones not loaded yet">
              Select all matching
            </button>
            <button className="btn-ghost" onClick={deselectAll} disabled={!selected?.size}>Deselect all</button>
          </>
        )}
          <button className={selected ? "btn-primary" : "btn-ghost"} onClick={() => setMode(!selected)}>
            {selected ? "Done selecting" : "Select images"}
          </button>
        </div>
      )}
      <InfiniteGrid list={list} layout="square" empty="No images."
                    render={(img, i) => (
                      <ImageTile image={img} onOpen={() => setOpen(i)}
                                 selected={selected?.has(img.id) || marquee.preview.has(img.id)} onToggle={selected ? toggle : undefined} />
                    )} />
      {/* The same selection buttons below a page, so they're at hand after scrolling down. */}
      {canCreate && list.paging && list.items.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center justify-center gap-2" data-testid="select-bottom">
          <button className={selected ? "btn-primary" : "btn-ghost"} onClick={() => setMode(!selected)}>
            {selected ? "Done selecting" : "Select images"}
          </button>
          {selected && (
            <>
              <button className="btn-ghost" onClick={selectShown}>Select this page</button>
              <button className="btn-ghost" onClick={selectAllMatching}>Select all matching</button>
              <button className="btn-ghost" onClick={deselectAll} disabled={!selected?.size}>Deselect all</button>
            </>
          )}
        </div>
      )}
      {selected && selected.size > 0 && (
        <BulkBar kind="images" ids={[...selected]} note={note} onDone={() => setSelected(new Set())}
                 onSelectAll={selectShown} />
      )}
      {/* In paged mode the lightbox steps through the current page only. */}
      {open !== null && (
        <Lightbox images={list.items} index={open} onIndex={setOpen} onClose={() => setOpen(null)}
                  total={list.paging ? list.items.length : list.total}
                  onNearEnd={() => !list.paging && list.hasNextPage && !list.isFetchingNextPage && list.fetchNextPage()} />
      )}
    </>
  );
}
