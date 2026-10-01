import type { Playback, PlaybackMode } from "../api/types";

const VIDEO_CODECS: Record<string, string> = {
  h264: "avc1.640028",
  hevc: "hvc1.1.6.L120.90",
  vp8: "vp8",
  vp9: "vp09.00.10.08",
  av1: "av01.0.08M.08",
};
const AUDIO_CODECS: Record<string, string> = {
  aac: "mp4a.40.2",
  mp3: "mp3",
  opus: "opus",
  vorbis: "vorbis",
  flac: "flac",
};

type CanPlay = (type: string) => string;

function mimeFor(container: string, video: string | null, audio: string | null): string | null {
  const v = video ? VIDEO_CODECS[video] : undefined;
  if (!v) return null;
  const a = audio ? AUDIO_CODECS[audio] : undefined;
  if (audio && !a) return null;
  return `${container}; codecs="${[v, a].filter(Boolean).join(", ")}"`;
}

/** Delivery modes this browser can actually use, in the server's preference order. */
export function usableModes(p: Playback, canPlay: CanPlay): PlaybackMode[] {
  return p.modes.filter((mode) => {
    if (mode === "transcode") return true; // H.264 + AAC in MP4 plays everywhere
    if (mode === "remux") {
      const mime = mimeFor("video/mp4", p.video_codec, "aac"); // remux re-encodes audio to AAC
      return !!mime && canPlay(mime) !== "";
    }
    const mime = mimeFor(p.direct.mime_type, p.video_codec, p.audio_codec);
    return !!mime && canPlay(mime) !== "";
  });
}

export interface Source {
  mode: PlaybackMode;
  height: number | null;
}

/** URL for a source; converted streams start at `offset` seconds (they can't byte-seek). */
export function sourceUrl(p: Playback, src: Source, offset: number): string {
  if (src.mode === "direct") return p.direct.url;
  const base = src.mode === "remux" ? p.remux_url : p.transcode_url;
  const params = new URLSearchParams();
  if (offset > 0) params.set("start", offset.toFixed(3));
  if (src.mode === "transcode" && src.height) params.set("height", String(src.height));
  const qs = params.toString();
  return qs ? `${base}&${qs}` : base;
}

export function sourceLabel(src: Source): string {
  if (src.mode === "transcode") return src.height ? `${src.height}p` : "Compatible";
  return "Original";
}
