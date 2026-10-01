import { useEffect, useRef, useState } from "react";
import type { ImageItem } from "../api/types";

/**
 * Full-screen image viewer. ←/→ or swipe to navigate, click to toggle zoom, Esc to close.
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
  const swipe = useRef<number | null>(null);
  const image = images[index];

  const go = (delta: number) => {
    const next = index + delta;
    if (next >= 0 && next < images.length) onIndex(next);
  };

  useEffect(() => {
    setZoom(false);
    setLoaded(false);
    for (const i of [index + 1, index + 2, index - 1]) {
      if (images[i]) new Image().src = images[i].file_url;
    }
    if (index >= images.length - 5) onNearEnd?.();
  }, [index]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
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

  if (!image) return null;
  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-black/95" role="dialog" aria-label="Image viewer" data-testid="lightbox">
      <div className="flex items-center gap-4 px-4 py-2 text-sm text-zinc-300">
        <span className="tabular-nums" data-testid="lightbox-counter">{index + 1} / {total ?? images.length}</span>
        <span className="truncate">{image.title}</span>
        <span className="text-zinc-500">{image.width}×{image.height}</span>
        <div className="flex-1" />
        <a href={image.file_url} target="_blank" rel="noreferrer" className="hover:text-white">Original</a>
        <button onClick={onClose} className="text-xl hover:text-white" aria-label="Close">✕</button>
      </div>
      <div className={`relative flex-1 ${zoom ? "overflow-auto" : "overflow-hidden"}`}
           onPointerDown={(e) => { swipe.current = e.clientX; }}
           onPointerUp={(e) => {
             const dx = swipe.current == null ? 0 : e.clientX - swipe.current;
             swipe.current = null;
             if (Math.abs(dx) > 60 && !zoom) go(dx < 0 ? 1 : -1);
           }}>
        {!loaded && <img src={image.thumbnail_url} alt="" className="absolute inset-0 m-auto max-h-full max-w-full object-contain opacity-60 blur-sm" />}
        <img key={image.id} src={image.file_url} alt={image.title ?? ""} onLoad={() => setLoaded(true)}
             onClick={() => setZoom((z) => !z)}
             className={zoom ? "mx-auto max-w-none cursor-zoom-out" : "absolute inset-0 m-auto max-h-full max-w-full cursor-zoom-in object-contain"} />
        {index > 0 && (
          <button onClick={() => go(-1)} aria-label="Previous"
                  className="absolute left-2 top-1/2 -translate-y-1/2 rounded-full bg-black/50 px-3 py-2 text-2xl text-white hover:bg-black/80">‹</button>
        )}
        {index < images.length - 1 && (
          <button onClick={() => go(1)} aria-label="Next"
                  className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full bg-black/50 px-3 py-2 text-2xl text-white hover:bg-black/80">›</button>
        )}
      </div>
    </div>
  );
}
