import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { useDetail } from "../api/hooks";
import type { ImageDetail, ImageItem } from "../api/types";
import { formatBytes } from "../lib/format";
import { releaseFocus } from "../lib/useEscape";
import { ContentEditModal } from "./EditForms";

/**
 * Full-screen image viewer. ←/→ or swipe to navigate, click the picture to toggle zoom;
 * Esc, the ✕, or a click outside the picture closes it.
 * Neighbours are preloaded so stepping through a gallery is instant; `onNearEnd` lets the
 * caller fetch the next page before the user reaches the end of what's loaded.
 */
export default function Lightbox({ images, index, onIndex, onClose, onNearEnd, total }: {
  images: ImageItem[];
  index: number;
  onIndex: (i: number) => void;
  onClose: () => void;
  onNearEnd?: () => void;
  total?: number | null;
}) {
  const [zoom, setZoom] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [editing, setEditing] = useState(false);
  // A Live Photo's motion, played over the still on request (LIVE button or holding the picture).
  const [moving, setMoving] = useState(false);
  const swipe = useRef<number | null>(null);
  const swiped = useRef(false);
  const frame = useRef<HTMLDivElement>(null);
  const image = images[index];
  const detail = useDetail<ImageDetail>(image ? `/images/${image.id}` : null).data;

  const go = (delta: number) => {
    const next = index + delta;
    if (next >= 0 && next < images.length) onIndex(next);
  };

  useEffect(() => {
    setZoom(false);
    setLoaded(false);
    setMoving(false);
    for (const i of [index + 1, index + 2, index - 1]) {
      if (images[i]) new Image().src = images[i].file_url;
    }
    if (index >= images.length - 5) onNearEnd?.();
  }, [index]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // While the edit dialog is open, keys belong to it. Otherwise the viewer owns the
      // keyboard regardless of what had focus behind it: some browsers leave focus in,
      // say, the search box when a thumbnail is clicked.
      if (editing) return;
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowRight" || e.key === " ") { e.preventDefault(); go(1); }
      else if (e.key === "ArrowLeft") go(-1);
    };
    window.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  });

  // Take the keyboard when opening, so keys can't land in a field behind the viewer.
  useEffect(() => {
    releaseFocus();
    frame.current?.focus();
  }, []);

  if (!image) return null;
  return (
    <div ref={frame} tabIndex={-1} className="fixed inset-0 z-50 flex flex-col bg-bg/95 outline-none"
         role="dialog" aria-modal="true" aria-label="Image viewer" data-testid="lightbox">
      <div className="flex items-center gap-4 px-4 py-2 text-sm text-text">
        <span className="tabular-nums" data-testid="lightbox-counter">{index + 1} / {total ?? images.length}</span>
        <span className="truncate">{image.title}</span>
        <span className="text-text-subtle">{image.width}×{image.height}</span>
        {detail && detail.id === image.id && (
          <span className="flex min-w-0 flex-wrap items-center gap-1.5" data-testid="lightbox-meta">
            {detail.performers.map((p) => (
              <Link key={`p${p.id}`} to={`/performers/${p.id}`} className="chip bg-accent/20 text-text">{p.name}</Link>
            ))}
            {detail.tags.map((t) => <Link key={`t${t.id}`} to={`/tags/${t.id}`} className="chip">{t.name}</Link>)}
          </span>
        )}
        <div className="flex-1" />
        {detail?.can_edit && detail.id === image.id && (
          <button onClick={() => setEditing(true)} className="hover:text-text">Edit</button>
        )}
        {image.live_url && (
          <button onClick={() => setMoving((m) => !m)} aria-pressed={moving} data-testid="live-button"
                  className={`rounded px-2 py-0.5 text-xs font-semibold tracking-wide ${moving ? "bg-text text-bg" : "bg-text/15 text-text hover:bg-text/25"}`}>
            LIVE
          </button>
        )}
        <a href={`${image.file_url}${image.file_url.includes("?") ? "&" : "?"}raw=1`} target="_blank" rel="noreferrer" className="hover:text-text">Original</a>
        <button onClick={onClose} className="text-xl hover:text-text" aria-label="Close">✕</button>
      </div>
      <div className={`relative flex-1 ${zoom ? "overflow-auto" : "overflow-hidden"}`}
           onPointerDown={(e) => { swipe.current = e.clientX; }}
           onPointerUp={(e) => {
             const dx = swipe.current == null ? 0 : e.clientX - swipe.current;
             swipe.current = null;
             swiped.current = Math.abs(dx) > 60;
             if (swiped.current && !zoom) go(dx < 0 ? 1 : -1);
           }}
           onClick={(e) => {
             // A click on the empty area around the picture closes the viewer (the picture
             // itself toggles zoom, the arrows navigate). The end of a swipe is not a click.
             const onBackdrop = e.target === e.currentTarget || (e.target as HTMLElement).dataset.backdrop === "1";
             if (onBackdrop && !swiped.current) onClose();
           }} data-testid="lightbox-backdrop">
        {!loaded && <img src={image.thumbnail_url} alt="" data-backdrop="1" className="absolute inset-0 m-auto max-h-full max-w-full object-contain opacity-60 blur-sm" />}
        <img key={image.id} src={image.file_url} alt={image.title ?? ""} onLoad={() => setLoaded(true)}
             onContextMenu={(e) => { if (image.live_url) { e.preventDefault(); setMoving(true); } }}
             onClick={() => setZoom((z) => !z)}
             className={zoom ? "mx-auto max-w-none cursor-zoom-out" : "absolute inset-0 m-auto max-h-full max-w-full cursor-zoom-in object-contain"} />
        {moving && image.live_url && (
          <video key={image.live_url} src={image.live_url} autoPlay playsInline data-testid="live-video"
                 onEnded={() => setMoving(false)} onError={() => setMoving(false)} onClick={() => setMoving(false)}
                 className="absolute inset-0 m-auto max-h-full max-w-full object-contain" />
        )}
        {index > 0 && (
          <button onClick={() => go(-1)} aria-label="Previous"
                  className="absolute left-2 top-1/2 -translate-y-1/2 rounded-full bg-overlay px-3 py-2 text-2xl text-text hover:bg-bg">‹</button>
        )}
        {index < images.length - 1 && (
          <button onClick={() => go(1)} aria-label="Next"
                  className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full bg-overlay px-3 py-2 text-2xl text-text hover:bg-bg">›</button>
        )}
      </div>
      {detail && detail.id === image.id && detail.file?.path && (
        <div className="flex flex-wrap items-center gap-x-3 px-4 py-1.5 text-xs text-text-muted" data-testid="lightbox-file">
          <span className="uppercase tracking-wide text-text-subtle">File</span>
          <span className="select-all break-all font-mono">
            {detail.file.zip_path ? `${detail.file.zip_path} → ${detail.file.path}` : detail.file.path}
          </span>
          <span className="font-mono">{formatBytes(detail.file.size)}</span>
        </div>
      )}
      {editing && detail && <ContentEditModal kind="images" item={detail} onClose={() => setEditing(false)} />}
    </div>
  );
}
