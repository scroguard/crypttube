import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

export interface CropRect { x: number; y: number; w: number; h: number }   // fractions of the picture

const FRAME_WIDTH = 320;
const MAX_ZOOM = 5;

/**
 * Position a picture inside a frame of the given aspect ratio (width / height): drag to
 * move, slider or mouse wheel to zoom. The picture always covers the frame. Reports the
 * visible part as fractions of the picture.
 */
export default function ImageCropper({ src, aspect, onChange }: {
  src: string; aspect: number; onChange: (rect: CropRect) => void;
}) {
  const frameW = FRAME_WIDTH;
  const frameH = Math.round(FRAME_WIDTH / aspect);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [zoom, setZoom] = useState(1);                    // 1 = the picture just covers the frame
  const [pos, setPos] = useState({ x: 0, y: 0 });         // top-left of the picture, frame pixels
  const drag = useRef<{ px: number; py: number; x: number; y: number } | null>(null);

  const cover = natural ? Math.max(frameW / natural.w, frameH / natural.h) : 1;
  const scale = cover * zoom;
  const shownW = natural ? natural.w * scale : 0;
  const shownH = natural ? natural.h * scale : 0;
  const clamp = (x: number, y: number, w = shownW, h = shownH) => ({
    x: Math.min(0, Math.max(frameW - w, x)),
    y: Math.min(0, Math.max(frameH - h, y)),
  });

  // Start centred.
  useEffect(() => {
    if (!natural) return;
    const s = Math.max(frameW / natural.w, frameH / natural.h);
    setZoom(1);
    setPos({ x: (frameW - natural.w * s) / 2, y: (frameH - natural.h * s) / 2 });
  }, [natural, frameW, frameH]);

  useEffect(() => {
    if (!natural || !shownW) return;
    onChange({ x: -pos.x / shownW, y: -pos.y / shownH, w: frameW / shownW, h: frameH / shownH });
  }, [pos, shownW, shownH]); // eslint-disable-line react-hooks/exhaustive-deps

  function zoomTo(next: number) {
    if (!natural) return;
    const z = Math.min(MAX_ZOOM, Math.max(1, next));
    const s = cover * z;
    // keep the point in the middle of the frame where it is
    const cx = (frameW / 2 - pos.x) / scale, cy = (frameH / 2 - pos.y) / scale;
    setZoom(z);
    setPos(clamp(frameW / 2 - cx * s, frameH / 2 - cy * s, natural.w * s, natural.h * s));
  }

  const down = (e: ReactPointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { px: e.clientX, py: e.clientY, x: pos.x, y: pos.y };
  };
  const move = (e: ReactPointerEvent) => {
    const d = drag.current;
    if (d) setPos(clamp(d.x + e.clientX - d.px, d.y + e.clientY - d.py));
  };

  return (
    <div className="flex flex-col items-center gap-3">
      <div className="relative cursor-grab touch-none select-none overflow-hidden rounded-lg bg-bg ring-1 ring-border-strong active:cursor-grabbing"
           style={{ width: frameW, height: frameH }} data-testid="crop-frame"
           onPointerDown={down} onPointerMove={move} onPointerUp={() => { drag.current = null; }}
           onWheel={(e) => zoomTo(zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1))}>
        <img src={src} alt="" draggable={false} onLoad={(e) => setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
             className="pointer-events-none absolute max-w-none" style={{ left: pos.x, top: pos.y, width: shownW || undefined, height: shownH || undefined }} />
      </div>
      <label className="flex w-full max-w-xs items-center gap-2 text-xs text-text-muted">
        Zoom
        <input type="range" min={1} max={MAX_ZOOM} step={0.01} value={zoom} aria-label="Zoom"
               onChange={(e) => zoomTo(Number(e.target.value))} className="flex-1 accent-accent" />
      </label>
      <p className="text-xs text-text-subtle">Drag the picture to choose what shows; zoom to get closer.</p>
    </div>
  );
}
