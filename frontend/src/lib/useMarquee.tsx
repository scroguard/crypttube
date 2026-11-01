import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

const THRESHOLD = 6;      // px of movement before a press becomes a drag (a click stays a click)
const EDGE = 48;          // px from the window edge where dragging scrolls the page
const MAX_SPEED = 24;     // px per frame

interface Box { x1: number; y1: number; x2: number; y2: number }   // page coordinates

/**
 * Rubber-band selection: press the mouse anywhere (also on an item) and drag to draw a
 * box; every item it touches is added to the selection when the button is released.
 * Items are found by their `data-select-id` attribute. Dragging near the top or bottom
 * of the window scrolls; items that scroll out of view (and out of the virtualized DOM)
 * keep counting, because their positions are remembered in page coordinates.
 * Mouse and pen only: on touch screens a drag scrolls the page.
 */
export function useMarquee(enabled: boolean, onAdd: (ids: number[]) => void): { preview: Set<number>; overlay: ReactNode } {
  const [box, setBox] = useState<Box | null>(null);
  const [preview, setPreview] = useState<Set<number>>(() => new Set());
  const add = useRef(onAdd);
  add.current = onAdd;

  useEffect(() => {
    if (!enabled) return;
    let start: { x: number; y: number } | null = null;
    let pointer = { x: 0, y: 0 };            // viewport coordinates of the mouse
    let dragging = false;
    let frame = 0;
    let hits = new Set<number>();
    const seen = new Map<number, DOMRect>();  // id -> rectangle in page coordinates

    const measure = () => {
      for (const el of document.querySelectorAll<HTMLElement>("[data-select-id]")) {
        const r = el.getBoundingClientRect();
        seen.set(Number(el.dataset.selectId), new DOMRect(r.left + scrollX, r.top + scrollY, r.width, r.height));
      }
    };
    const update = () => {
      if (!start) return;
      const b = { x1: Math.min(start.x, pointer.x + scrollX), y1: Math.min(start.y, pointer.y + scrollY),
                  x2: Math.max(start.x, pointer.x + scrollX), y2: Math.max(start.y, pointer.y + scrollY) };
      measure();
      hits = new Set();
      for (const [id, r] of seen) {
        if (r.right >= b.x1 && r.left <= b.x2 && r.bottom >= b.y1 && r.top <= b.y2) hits.add(id);
      }
      setBox(b);
      setPreview(hits);
    };
    const autoscroll = () => {
      const dy = pointer.y < EDGE ? -Math.ceil(MAX_SPEED * (1 - pointer.y / EDGE))
        : pointer.y > innerHeight - EDGE ? Math.ceil(MAX_SPEED * (1 - (innerHeight - pointer.y) / EDGE)) : 0;
      if (dy) { scrollBy(0, dy); update(); }
      frame = requestAnimationFrame(autoscroll);
    };
    const stop = () => {
      cancelAnimationFrame(frame);
      start = null;
      dragging = false;
      seen.clear();
      setBox(null);
      setPreview(new Set());
      document.body.style.userSelect = "";
    };

    const onDown = (e: PointerEvent) => {
      if (e.button !== 0 || e.pointerType === "touch" || e.shiftKey) return;
      const target = e.target as HTMLElement;
      // Controls keep their own behaviour (search box, buttons in the bars, menus…).
      if (target.closest("input, textarea, select, nav, [data-no-marquee], [role=dialog], [role=listbox]")) return;
      if (target.closest("button, a") && !target.closest("[data-select-id], [data-select-locked]")) return;
      start = { x: e.clientX + scrollX, y: e.clientY + scrollY };
      pointer = { x: e.clientX, y: e.clientY };
    };
    const onMove = (e: PointerEvent) => {
      if (!start) return;
      pointer = { x: e.clientX, y: e.clientY };
      if (!dragging) {
        if (Math.hypot(e.clientX + scrollX - start.x, e.clientY + scrollY - start.y) < THRESHOLD) return;
        dragging = true;
        document.body.style.userSelect = "none";
        frame = requestAnimationFrame(autoscroll);
      }
      e.preventDefault();
      update();
    };
    const onUp = () => {
      if (dragging) {
        const ids = [...hits];
        // The release ends on some item; that click must not toggle it back.
        const swallow = (ev: MouseEvent) => { ev.preventDefault(); ev.stopPropagation(); };
        window.addEventListener("click", swallow, { capture: true, once: true });
        setTimeout(() => window.removeEventListener("click", swallow, { capture: true }), 0);
        if (ids.length) add.current(ids);
      }
      stop();
    };
    // No native image/link drag-and-drop while selecting: it would hijack the gesture.
    const onDragStart = (e: DragEvent) => {
      if ((e.target as HTMLElement).closest?.("[data-select-id], [data-select-locked]")) e.preventDefault();
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && dragging && stop();

    window.addEventListener("pointerdown", onDown);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", stop);
    window.addEventListener("dragstart", onDragStart);
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", stop);
      window.removeEventListener("dragstart", onDragStart);
      window.removeEventListener("keydown", onKey, true);
      stop();
    };
  }, [enabled]);

  const overlay = box ? createPortal(
    <div data-testid="marquee" aria-hidden
         className="pointer-events-none absolute z-30 rounded-sm border-2 border-accent bg-accent/20"
         style={{ left: box.x1, top: box.y1, width: box.x2 - box.x1, height: box.y2 - box.y1 }} />,
    document.body) : null;
  return { preview, overlay };
}
