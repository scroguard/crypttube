import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type { Playback } from "../api/types";
import { formatDuration } from "../lib/format";
import { sourceLabel, sourceUrl, usableModes, type Source } from "../lib/playback";
import { cueAt, loadSprites, type SpriteCue } from "../lib/vtt";

const RESUME_SAVE_MS = 10_000;
const HIDE_CONTROLS_MS = 2500;
const VOLUME_KEY = "player.volume";

function readVolume(): { volume: number; muted: boolean } {
  try {
    const v = JSON.parse(localStorage.getItem(VOLUME_KEY) ?? "");
    if (typeof v.volume === "number") return { volume: Math.min(1, Math.max(0, v.volume)), muted: !!v.muted };
  } catch { /* unset or unavailable */ }
  return { volume: 1, muted: false };
}

function saveResume(sceneId: number, time: number | null, keepalive = false) {
  // keepalive lets the final save complete while the page is being left.
  return fetch(`/api/scenes/${sceneId}/resume`, {
    method: "PUT", keepalive, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-With": "CryptTube" },
    body: JSON.stringify({ time: time == null ? null : Math.max(0, time) }),
  }).catch(() => undefined);
}

type WebkitDocument = Document & { webkitFullscreenElement?: Element | null; webkitExitFullscreen?: () => void };
type WebkitElement = HTMLElement & { webkitRequestFullscreen?: () => void };
type WebkitVideo = HTMLVideoElement & { webkitEnterFullscreen?: () => void };
const fullscreenElement = () => document.fullscreenElement ?? (document as WebkitDocument).webkitFullscreenElement ?? null;

export default function VideoPlayer({ sceneId, playback, poster, resumeTime }: {
  sceneId: number; playback: Playback; poster: string; resumeTime: number | null;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const container = useRef<HTMLDivElement>(null);
  const duration = playback.duration ?? 0;

  const modes = useMemo(() => {
    const probe = document.createElement("video");
    return usableModes(playback, (t) => probe.canPlayType(t));
  }, [playback]);
  const originalMode = modes.find((m) => m !== "transcode");
  const startAt = resumeTime && resumeTime > 5 && resumeTime < duration - 10 ? resumeTime : 0;

  const [source, setSource] = useState<Source>({ mode: modes[0], height: null });
  // Converted streams can't byte-seek: they are requested starting at `offset`.
  const [offset, setOffset] = useState(source.mode === "direct" ? 0 : startAt);
  const pendingSeek = useRef<number | null>(source.mode === "direct" && startAt ? startAt : null);
  const wantPlay = useRef(false);
  const played = useRef(false);

  const [time, setTime] = useState(startAt);
  const [bufferedEnd, setBufferedEnd] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [{ volume, muted }, setVol] = useState(readVolume);
  const [fullscreen, setFullscreen] = useState(false);
  const [controls, setControls] = useState(true);
  const [menu, setMenu] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cues, setCues] = useState<SpriteCue[]>([]);
  const [hover, setHover] = useState<{ x: number; time: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  const base = source.mode === "direct" ? 0 : offset;
  const src = sourceUrl(playback, source, offset);
  const absTime = useCallback(() => base + (video.current?.currentTime ?? 0), [base]);

  useEffect(() => {
    if (playback.sprite_vtt_url) loadSprites(playback.sprite_vtt_url).then(setCues);
  }, [playback.sprite_vtt_url]);

  useEffect(() => {
    const v = video.current;
    if (!v) return;
    v.volume = volume;
    v.muted = muted;
    try { localStorage.setItem(VOLUME_KEY, JSON.stringify({ volume, muted })); } catch { /* ignore */ }
  }, [volume, muted]);

  // Periodic + final resume saves.
  useEffect(() => {
    if (!playing) return;
    const t = setInterval(() => saveResume(sceneId, absTime()), RESUME_SAVE_MS);
    return () => clearInterval(t);
  }, [playing, sceneId, absTime]);
  const latest = useRef({ absTime, started: false });
  latest.current = { absTime, started: played.current };
  useEffect(() => () => {
    if (latest.current.started) saveResume(sceneId, latest.current.absTime(), true);
  }, [sceneId]);

  useEffect(() => {
    const onFs = () => setFullscreen(fullscreenElement() === container.current);
    document.addEventListener("fullscreenchange", onFs);
    document.addEventListener("webkitfullscreenchange", onFs);
    return () => {
      document.removeEventListener("fullscreenchange", onFs);
      document.removeEventListener("webkitfullscreenchange", onFs);
    };
  }, []);

  // Auto-hide controls while playing.
  const hideTimer = useRef<number>(undefined);
  const poke = useCallback(() => {
    setControls(true);
    window.clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => setControls(false), HIDE_CONTROLS_MS);
  }, []);
  const showControls = controls || !playing || menu || dragging;

  const seek = useCallback((t: number) => {
    const target = Math.min(Math.max(0, t), Math.max(0, duration - 0.25));
    setTime(target);
    if (source.mode === "direct") {
      if (video.current) video.current.currentTime = target;
    } else {
      wantPlay.current = playing || wantPlay.current;
      setOffset(target);
    }
  }, [duration, source.mode, playing]);

  const toggle = useCallback(() => {
    const v = video.current;
    if (!v) return;
    if (v.paused) { wantPlay.current = true; v.play().catch(() => undefined); }
    else v.pause();
  }, []);

  const toggleFullscreen = useCallback(() => {
    // Phones differ: iPhone Safari has no fullscreen for page elements at all, only its
    // own video player (webkitEnterFullscreen on the <video>); older iPad Safari has the
    // prefixed API. Use whichever exists, the standard one first.
    const doc = document as WebkitDocument;
    const box = container.current as WebkitElement | null;
    const v = video.current as WebkitVideo | null;
    if (fullscreenElement()) {
      if (document.exitFullscreen) document.exitFullscreen().catch(() => undefined);
      else doc.webkitExitFullscreen?.();
    } else if (box?.requestFullscreen) {
      box.requestFullscreen().catch(() => v?.webkitEnterFullscreen?.());
    } else if (box?.webkitRequestFullscreen) {
      box.webkitRequestFullscreen();
    } else {
      v?.webkitEnterFullscreen?.();
    }
  }, []);

  function choose(next: Source) {
    setMenu(false);
    if (next.mode === source.mode && next.height === source.height) return;
    const t = absTime();
    wantPlay.current = playing;
    if (next.mode === "direct") pendingSeek.current = t;
    setOffset(next.mode === "direct" ? 0 : t);
    setSource(next);
  }

  // Keyboard shortcuts (ignored while typing in form fields).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = e.target as HTMLElement;
      if (el.closest("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key.toLowerCase();
      const actions: Record<string, () => void> = {
        " ": toggle, k: toggle,
        arrowleft: () => seek(absTime() - 5), arrowright: () => seek(absTime() + 5),
        j: () => seek(absTime() - 10), l: () => seek(absTime() + 10),
        arrowup: () => setVol((s) => ({ volume: Math.min(1, s.volume + 0.1), muted: false })),
        arrowdown: () => setVol((s) => ({ ...s, volume: Math.max(0, s.volume - 0.1) })),
        m: () => setVol((s) => ({ ...s, muted: !s.muted })),
        f: toggleFullscreen,
      };
      const action = actions[k];
      if (action) { e.preventDefault(); action(); poke(); }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggle, seek, absTime, toggleFullscreen, poke]);

  const events = {
    onLoadedMetadata: () => {
      const v = video.current!;
      if (pendingSeek.current != null) { v.currentTime = pendingSeek.current; pendingSeek.current = null; }
      if (wantPlay.current) v.play().catch(() => undefined);
    },
    onTimeUpdate: () => {
      const v = video.current!;
      if (!dragging) setTime(base + v.currentTime);
      const b = v.buffered;
      for (let i = 0; i < b.length; i++) {
        if (b.start(i) <= v.currentTime + 0.5 && b.end(i) >= v.currentTime) setBufferedEnd(base + b.end(i));
      }
    },
    onPlay: () => { setPlaying(true); poke(); },
    onPause: () => { setPlaying(false); if (played.current) saveResume(sceneId, absTime()); },
    onPlaying: () => {
      setWaiting(false);
      setError(null);
      if (!played.current) { played.current = true; api(`/scenes/${sceneId}/play`, { method: "POST" }).catch(() => undefined); }
    },
    onWaiting: () => setWaiting(true),
    onCanPlay: () => setWaiting(false),
    onEnded: () => { setPlaying(false); saveResume(sceneId, null); },
    onError: () => {
      // Fall back to the next delivery mode, keeping the position.
      const next = modes[modes.indexOf(source.mode) + 1];
      if (next) {
        // where the video actually is (the displayed time can lag behind or be a seek target)
        const t = absTime();
        wantPlay.current = true;
        setOffset(next === "direct" ? 0 : t);
        setSource({ mode: next, height: null });
      } else setError("This video can't be played in this browser.");
    },
  };

  // Seek bar geometry.
  const bar = useRef<HTMLDivElement>(null);
  function timeAt(clientX: number) {
    const r = bar.current!.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
    return { x: frac * r.width, time: frac * duration };
  }
  const preview = hover ? cueAt(cues, hover.time) : null;
  const pct = (t: number) => `${duration ? Math.min(100, (t / duration) * 100) : 0}%`;
  const qualityOptions: Source[] = [
    ...(originalMode ? [{ mode: originalMode, height: null } as Source] : []),
    ...playback.transcode_heights.slice().reverse().map((h) => ({ mode: "transcode", height: h }) as Source),
    ...(!originalMode || !playback.transcode_heights.length ? [{ mode: "transcode", height: null } as Source] : []),
  ];

  return (
    <div ref={container} data-testid="player"
         className={`group relative aspect-video w-full overflow-hidden bg-black ${fullscreen ? "" : "rounded-lg"} ${showControls ? "" : "cursor-none"}`}
         onMouseMove={poke} onMouseLeave={() => playing && setControls(false)}>
      <video ref={video} src={src} poster={poster} preload="metadata" playsInline
             className="h-full w-full" onClick={toggle} onDoubleClick={toggleFullscreen} {...events} />

      {waiting && playing && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <div className="h-12 w-12 animate-spin rounded-full border-4 border-white/20 border-t-white" />
        </div>
      )}
      {!playing && !waiting && (
        <button onClick={toggle} aria-label="Play"
                className="absolute left-1/2 top-1/2 flex h-16 w-16 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full bg-black/60 text-3xl text-white hover:bg-accent">
          ▶
        </button>
      )}
      {error && <div className="absolute inset-x-0 top-4 mx-auto w-fit rounded bg-red-900/90 px-4 py-2 text-sm">{error}</div>}

      <div className={`absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/90 via-black/50 to-transparent px-3 pb-2 pt-10 transition-opacity ${showControls ? "opacity-100" : "pointer-events-none opacity-0"}`}>
        {/* Seek bar */}
        <div ref={bar} className="relative h-4 cursor-pointer touch-none" data-testid="seekbar"
             onPointerMove={(e) => { setHover(timeAt(e.clientX)); if (dragging) setTime(timeAt(e.clientX).time); }}
             onPointerLeave={() => !dragging && setHover(null)}
             onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); setDragging(true); setTime(timeAt(e.clientX).time); }}
             onPointerUp={(e) => { setDragging(false); seek(timeAt(e.clientX).time); }}>
          <div className="absolute inset-x-0 top-1/2 h-1 -translate-y-1/2 rounded bg-white/25 group-hover:h-1.5">
            <div className="absolute inset-y-0 left-0 rounded bg-white/40" style={{ width: pct(bufferedEnd) }} />
            <div className="absolute inset-y-0 left-0 rounded bg-accent" style={{ width: pct(time) }} />
          </div>
          <div className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent" style={{ left: pct(time) }} />
          {hover && (
            <div className="pointer-events-none absolute bottom-6 -translate-x-1/2 text-center"
                 style={{ left: Math.min(Math.max(hover.x, 90), (bar.current?.clientWidth ?? 0) - 90) }}>
              {preview && (
                <div className="mb-1 overflow-hidden rounded border border-white/30 bg-black" style={{
                  width: preview.w, height: preview.h, backgroundImage: `url(${preview.url})`,
                  backgroundPosition: `-${preview.x}px -${preview.y}px`,
                }} />
              )}
              <span className="rounded bg-black/80 px-1.5 py-0.5 text-xs">{formatDuration(hover.time)}</span>
            </div>
          )}
        </div>

        {/* Buttons */}
        <div className="mt-1 flex items-center gap-2 text-sm">
          <button onClick={toggle} className="w-8 text-xl" aria-label={playing ? "Pause" : "Play"}>{playing ? "❚❚" : "▶"}</button>
          <button onClick={() => seek(absTime() - 10)} className="px-1 text-xs text-zinc-300 hover:text-white" aria-label="Back 10 seconds">−10</button>
          <button onClick={() => seek(absTime() + 10)} className="px-1 text-xs text-zinc-300 hover:text-white" aria-label="Forward 10 seconds">+10</button>
          <button onClick={() => setVol((s) => ({ ...s, muted: !s.muted }))} className="w-7" aria-label={muted ? "Unmute" : "Mute"}>
            {muted || volume === 0 ? "🔇" : "🔊"}
          </button>
          <input type="range" min={0} max={1} step={0.05} value={muted ? 0 : volume} aria-label="Volume"
                 onChange={(e) => setVol({ volume: +e.target.value, muted: false })} className="w-20 accent-accent" />
          <span className="tabular-nums text-zinc-200" data-testid="time">{formatDuration(time)} / {formatDuration(duration)}</span>
          <div className="flex-1" />
          <div className="relative">
            <button onClick={() => setMenu((m) => !m)} className="rounded px-2 py-0.5 text-xs font-semibold hover:bg-white/10"
                    aria-haspopup="menu" aria-expanded={menu} data-testid="quality">
              {sourceLabel(source)}
            </button>
            {menu && (
              <div role="menu" className="absolute bottom-8 right-0 w-40 rounded-md border border-zinc-700 bg-zinc-900/95 py-1 shadow-xl">
                <div className="px-3 py-1 text-[11px] uppercase tracking-wide text-zinc-500">Quality</div>
                {qualityOptions.map((o) => {
                  const active = o.mode === source.mode && o.height === source.height;
                  return (
                    <button key={`${o.mode}-${o.height}`} role="menuitemradio" aria-checked={active} onClick={() => choose(o)}
                            className={`block w-full px-3 py-1.5 text-left text-sm hover:bg-zinc-800 ${active ? "text-accent-hover" : ""}`}>
                      {active ? "✓ " : ""}{sourceLabel(o)}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
          <button onClick={toggleFullscreen} className="w-8 text-lg" aria-label={fullscreen ? "Exit fullscreen" : "Fullscreen"}>
            {fullscreen ? "⤡" : "⤢"}
          </button>
        </div>
      </div>
    </div>
  );
}
