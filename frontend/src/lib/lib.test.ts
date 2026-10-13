import { describe, expect, it } from "vitest";
import type { Playback } from "../api/types";
import { buildUrl } from "../api/client";
import { SCENE_FILTERS, readFilters, toQuery, writeFilters } from "./filters";
import { age, formatBytes, formatDuration, resolutionLabel } from "./format";
import { sourceUrl, usableModes } from "./playback";
import { isMediaFile } from "./upload";
import { parsePageSize } from "./pageSize";
import { cueAt, parseSpriteVtt } from "./vtt";

describe("format", () => {
  it("durations", () => {
    expect(formatDuration(5)).toBe("0:05");
    expect(formatDuration(65)).toBe("1:05");
    expect(formatDuration(3725)).toBe("1:02:05");
    expect(formatDuration(null)).toBe("–");
  });
  it("bytes and resolution", () => {
    expect(formatBytes(1176366984)).toBe("1.1 GB");
    expect(formatBytes(512)).toBe("512 B");
    expect(resolutionLabel(2160)).toBe("4K");
    expect(resolutionLabel(720)).toBe("720p");
  });
  it("age counts birthdays correctly", () => {
    expect(age("2000-06-15", new Date("2026-06-14T12:00:00"))).toBe(25);
    expect(age("2000-06-15", new Date("2026-06-15T12:00:00"))).toBe(26);
  });
});

describe("sprite vtt", () => {
  const vtt = `WEBVTT

00:00:00.000 --> 00:00:07.407
sprite.webp?v=abc#xywh=0,0,160,90

00:00:07.407 --> 00:00:14.815
sprite.webp?v=abc#xywh=160,0,160,90
`;
  const cues = parseSpriteVtt(vtt, "http://h/api/scenes/4/sprite.vtt?v=abc");
  it("parses cues and resolves urls against the vtt location", () => {
    expect(cues).toHaveLength(2);
    expect(cues[1]).toMatchObject({ x: 160, y: 0, w: 160, h: 90, url: "http://h/api/scenes/4/sprite.webp?v=abc" });
    expect(cues[1].start).toBeCloseTo(7.407);
  });
  it("finds the cue for a time", () => {
    expect(cueAt(cues, 0)?.x).toBe(0);
    expect(cueAt(cues, 10)?.x).toBe(160);
    expect(cueAt(cues, 999)?.x).toBe(160);
  });
});

describe("playback modes", () => {
  const base: Playback = {
    modes: ["direct", "remux", "transcode"],
    direct: { url: "/api/scenes/1/stream", mime_type: "video/mp4" },
    remux_url: "/api/scenes/1/stream.mp4?mode=remux",
    transcode_url: "/api/scenes/1/stream.mp4?mode=transcode",
    transcode_heights: [360, 720],
    video_codec: "hevc",
    audio_codec: "aac",
    duration: 60,
    screenshot_url: "",
    sprite_vtt_url: null, notice: null,
  };
  it("skips modes the browser can't decode", () => {
    const noHevc = (t: string) => (t.includes("hvc1") ? "" : "maybe");
    expect(usableModes(base, noHevc)).toEqual(["transcode"]);
    expect(usableModes(base, () => "probably")).toEqual(["direct", "remux", "transcode"]);
  });
  it("builds converted-stream urls with offset and height", () => {
    expect(sourceUrl(base, { mode: "direct", height: null }, 30)).toBe("/api/scenes/1/stream");
    expect(sourceUrl(base, { mode: "transcode", height: 720 }, 12.5)).toBe(
      "/api/scenes/1/stream.mp4?mode=transcode&start=12.500&height=720",
    );
    expect(sourceUrl(base, { mode: "remux", height: null }, 0)).toBe("/api/scenes/1/stream.mp4?mode=remux");
  });
});

describe("filters", () => {
  it("round-trips through the URL and maps to API params", () => {
    const values = { q: "beach", tags: [3, 4], desc: false, res: 1080, sort: "title" };
    const params = writeFilters(values, SCENE_FILTERS);
    expect(params.toString()).toBe("q=beach&sort=title&desc=0&tags=3&tags=4&res=1080");
    expect(readFilters(params, SCENE_FILTERS)).toEqual(values);
    expect(toQuery(values, SCENE_FILTERS)).toEqual({ q: "beach", sort: "title", desc: false, tag_ids: [3, 4], height_min: 1080 });
  });
  it("ignores junk", () => {
    expect(readFilters(new URLSearchParams("tags=x&res=abc&desc=maybe"), SCENE_FILTERS)).toEqual({});
  });
  it("builds api urls, repeating arrays and dropping empties", () => {
    expect(buildUrl("/scenes", { tag_ids: [1, 2], q: "", desc: false, x: undefined })).toBe(
      "/api/scenes?tag_ids=1&tag_ids=2&desc=false",
    );
  });
});

describe("page size preference", () => {
  it("accepts only offered sizes; anything else means continuous scrolling", () => {
    expect(parsePageSize("50")).toBe(50);
    expect(parsePageSize("200")).toBe(200);
    expect(parsePageSize("")).toBeNull();
    expect(parsePageSize(null)).toBeNull();
    expect(parsePageSize("7")).toBeNull();
    expect(parsePageSize("100000")).toBeNull();
  });
});

describe("upload file types", () => {
  const fromServer = ["heic", "jpg", "mov", "mp4", "zip"];
  it("follows the server's list, any case", () => {
    expect(isMediaFile("IMG_2702.HEIC", fromServer)).toBe(true);
    expect(isMediaFile("IMG_2702.mov", fromServer)).toBe(true);
    expect(isMediaFile("notes.txt", fromServer)).toBe(false);
    expect(isMediaFile(".hidden.jpg", fromServer)).toBe(false);
    expect(isMediaFile("noextension", fromServer)).toBe(false);
  });
});
