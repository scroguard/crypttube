import { memo, useRef, useState } from "react";
import { Link } from "react-router";
import type { GalleryItem, ImageItem, PerformerItem, Ref, SceneItem, StudioItem, TagItem } from "../api/types";
import { formatDate, formatDuration, plural, resolutionLabel } from "../lib/format";
import { cueAt, loadSprites, type SpriteCue } from "../lib/vtt";
import { Initials } from "./ui";

function Badge({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <span className={`rounded bg-black/75 px-1.5 py-0.5 text-[11px] font-semibold text-white ${className}`}>{children}</span>;
}

export function RefLinks({ items, to, max = 3 }: { items: Ref[]; to: string; max?: number }) {
  if (!items.length) return null;
  return (
    <span>
      {items.slice(0, max).map((r, i) => (
        <span key={r.id}>
          {i > 0 && ", "}
          <Link to={`/${to}/${r.id}`} className="hover:text-white">{r.name}</Link>
        </span>
      ))}
      {items.length > max && ` +${items.length - max}`}
    </span>
  );
}

function spriteUrl(scene: SceneItem): string {
  const v = new URL(scene.screenshot_url, location.origin).searchParams.get("v") ?? "";
  return `/api/scenes/${scene.id}/sprite.vtt?v=${encodeURIComponent(v)}`;
}

/** Hovering scrubs through the scene's sprite sheet (loaded on first hover only). */
function useHoverScrub(scene: SceneItem) {
  const [cues, setCues] = useState<SpriteCue[] | null>(null);
  const [cue, setCue] = useState<SpriteCue | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const handlers = {
    onMouseEnter: () => cues === null && scene.duration && loadSprites(spriteUrl(scene)).then(setCues),
    onMouseMove: (e: React.MouseEvent) => {
      if (!cues?.length || !scene.duration || !box.current) return;
      const r = box.current.getBoundingClientRect();
      const frac = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
      setCue(cueAt(cues, frac * scene.duration));
    },
    onMouseLeave: () => setCue(null),
  };
  return { box, cue, cues, handlers };
}

function SpriteFrame({ cue, cues, width }: { cue: SpriteCue; cues: SpriteCue[]; width: number }) {
  const sheetW = Math.max(...cues.map((c) => c.x + c.w));
  const sheetH = Math.max(...cues.map((c) => c.y + c.h));
  const scale = width / cue.w;
  return (
    <div className="absolute inset-0 flex items-center overflow-hidden bg-black">
      <div className="w-full" style={{
        height: cue.h * scale,
        backgroundImage: `url(${cue.url})`,
        backgroundSize: `${sheetW * scale}px ${sheetH * scale}px`,
        backgroundPosition: `-${cue.x * scale}px -${cue.y * scale}px`,
      }} />
    </div>
  );
}

export const SceneCard = memo(function SceneCard({ scene, selected, onToggle }: {
  scene: SceneItem; selected?: boolean; onToggle?: (id: number) => void;
}) {
  const { box, cue, cues, handlers } = useHoverScrub(scene);
  const res = resolutionLabel(scene.height);
  const progress = scene.resume_time && scene.duration ? Math.min(1, scene.resume_time / scene.duration) : 0;
  return (
    <div className="group" data-testid="scene-card" data-select-id={onToggle ? scene.id : undefined}>
      <Link to={`/scenes/${scene.id}`} onClick={(e) => { if (onToggle) { e.preventDefault(); onToggle(scene.id); } }}>
        <div ref={box} {...handlers}
             className={`relative aspect-video overflow-hidden rounded-lg bg-zinc-900 ring-2 ${selected ? "ring-accent" : "ring-transparent"}`}>
          <img src={scene.screenshot_url} alt="" loading="lazy" decoding="async"
               className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.02]" />
          {cue && cues && box.current && <SpriteFrame cue={cue} cues={cues} width={box.current.clientWidth} />}
          <div className="absolute left-1.5 top-1.5 flex gap-1">
            {res && <Badge>{res}</Badge>}
            {scene.missing && <Badge className="bg-red-800">missing</Badge>}
          </div>
          <Badge className="absolute bottom-1.5 right-1.5">{formatDuration(scene.duration)}</Badge>
          {progress > 0 && (
            <div className="absolute inset-x-0 bottom-0 h-1 bg-white/20"><div className="h-full bg-accent" style={{ width: `${progress * 100}%` }} /></div>
          )}
          {onToggle && (
            <div className={`absolute right-1.5 top-1.5 flex h-6 w-6 items-center justify-center rounded border-2 ${selected ? "border-accent bg-accent" : "border-white/80 bg-black/40"}`}>
              {selected && "✓"}
            </div>
          )}
        </div>
      </Link>
      <div className="mt-2 px-0.5">
        {/* Every card has the same height (two title lines, two detail lines, filled or
            not): the virtualized grid assumes equal rows, and uneven ones made the page
            jitter up and down while scrolling. */}
        <Link to={`/scenes/${scene.id}`} className="line-clamp-2 h-[2.75em] text-sm font-medium leading-snug hover:text-accent-hover" title={scene.title ?? ""}>
          {scene.title || "Untitled"}
        </Link>
        <div className="mt-0.5 h-4 truncate text-xs leading-4 text-zinc-400">
          {scene.studio && <Link to={`/studios/${scene.studio.id}`} className="hover:text-white">{scene.studio.name}</Link>}
          {scene.studio && scene.release_date && " · "}
          {scene.release_date && formatDate(scene.release_date)}
        </div>
        <div className="h-4 truncate text-xs leading-4 text-zinc-500">
          {scene.performers.length > 0 && <RefLinks items={scene.performers} to="performers" />}
        </div>
      </div>
    </div>
  );
});

export const GalleryCard = memo(function GalleryCard({ gallery }: { gallery: GalleryItem }) {
  return (
    <div className="group" data-testid="gallery-card">
      <Link to={`/galleries/${gallery.id}`} className="relative block aspect-video overflow-hidden rounded-lg bg-zinc-900">
        {gallery.cover_url && <img src={gallery.cover_url} alt="" loading="lazy" decoding="async" className="h-full w-full object-cover" />}
        <Badge className="absolute bottom-1.5 right-1.5">{plural(gallery.image_count, "image")}</Badge>
      </Link>
      <div className="mt-2 px-0.5">
        <Link to={`/galleries/${gallery.id}`} className="line-clamp-2 h-[2.75em] text-sm font-medium leading-snug hover:text-accent-hover">{gallery.title || "Untitled"}</Link>
        <div className="h-4 truncate text-xs leading-4 text-zinc-500"><RefLinks items={gallery.performers} to="performers" /></div>
      </div>
    </div>
  );
});

export const ImageTile = memo(function ImageTile({ image, onOpen, selected, onToggle }: {
  image: ImageItem; onOpen: () => void; selected?: boolean; onToggle?: (id: number) => void;
}) {
  return (
    <button onClick={() => (onToggle ? onToggle(image.id) : onOpen())} data-testid="image-tile" aria-pressed={onToggle ? !!selected : undefined}
            data-select-id={onToggle ? image.id : undefined}
            className={`relative block aspect-square w-full overflow-hidden rounded-md bg-zinc-900 ring-2 focus:outline-none focus:ring-accent ${selected ? "ring-accent" : "ring-transparent"}`}>
      <img src={image.thumbnail_url} alt={image.title ?? ""} loading="lazy" decoding="async"
           className="h-full w-full object-cover transition-transform duration-200 hover:scale-[1.03]" />
      {onToggle && (
        <span className={`absolute right-1.5 top-1.5 flex h-6 w-6 items-center justify-center rounded border-2 text-sm ${selected ? "border-accent bg-accent" : "border-white/80 bg-black/40"}`}>
          {selected && "✓"}
        </span>
      )}
    </button>
  );
});

export const PerformerCard = memo(function PerformerCard({ performer }: { performer: PerformerItem }) {
  return (
    <Link to={`/performers/${performer.id}`} className="group block" data-testid="performer-card">
      <div className="relative aspect-[2/3] overflow-hidden rounded-lg bg-zinc-900">
        {performer.image_url
          ? <img src={performer.image_url} alt="" loading="lazy" className="h-full w-full object-cover" />
          : <Initials name={performer.name} className="h-full w-full text-3xl" />}
        {performer.favorite && <span className="absolute right-1.5 top-1.5 text-accent" title="Favorite">♥</span>}
      </div>
      <div className="mt-1.5 truncate text-sm font-medium group-hover:text-accent-hover">
        {performer.name}
        {performer.disambiguation && <span className="text-zinc-500"> ({performer.disambiguation})</span>}
      </div>
      <div className="text-xs text-zinc-500">{plural(performer.scene_count, "scene")}</div>
    </Link>
  );
});

export const StudioCard = memo(function StudioCard({ studio }: { studio: StudioItem }) {
  return (
    <Link to={`/studios/${studio.id}`} className="card group block p-3 hover:border-zinc-600" data-testid="studio-card">
      <div className="flex aspect-[3/1] items-center justify-center overflow-hidden rounded bg-zinc-950">
        {studio.image_url
          ? <img src={studio.image_url} alt="" loading="lazy" className="max-h-full max-w-full object-contain" />
          : <span className="px-2 text-center font-semibold text-zinc-500">{studio.name}</span>}
      </div>
      <div className="mt-2 truncate text-sm font-medium group-hover:text-accent-hover">{studio.name}</div>
      <div className="truncate text-xs text-zinc-500">
        {studio.scene_count} scenes{studio.child_count > 0 && ` · ${studio.child_count} sub-studios`}
        {studio.parent && ` · ${studio.parent.name}`}
      </div>
    </Link>
  );
});

export const TagCard = memo(function TagCard({ tag }: { tag: TagItem }) {
  return (
    <Link to={`/tags/${tag.id}`} className="card group block p-3 hover:border-zinc-600" data-testid="tag-card">
      <div className="truncate font-medium group-hover:text-accent-hover">{tag.name}</div>
      <div className="truncate text-xs text-zinc-500">
        {tag.scene_count} scenes · {tag.gallery_count} galleries{tag.child_count > 0 && ` · ${tag.child_count} sub-tags`}
      </div>
    </Link>
  );
});
