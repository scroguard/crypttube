"""Media delivery: video streams, screenshots, scrub sprites, image thumbnails.

TODO(phase 5): enforce authentication and per-user ACLs on every route here.
"""

import asyncio
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from app import generated, jobs, streaming
from app.config import get_settings
from app.media import ProbeError

router = APIRouter(tags=["media"])

IMMUTABLE = "private, max-age=31536000, immutable"
REVALIDATE = "private, no-cache"


class VideoFileResponse(FileResponse):
    chunk_size = 1024 * 1024


_locks: dict[str, list] = {}


@asynccontextmanager
async def _keyed_lock(key: str):
    """Serialize work per key, so concurrent requests for the same missing asset
    generate it once. Entries are removed when nobody holds or awaits them."""
    entry = _locks.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            del _locks[key]


_gen_sem = asyncio.Semaphore(4)


async def _ensure(dest: Path, make) -> Path:
    """Return dest, generating it first (once, with bounded concurrency) if absent."""
    if dest.exists():
        return dest
    async with _keyed_lock(str(dest)):
        if not dest.exists():
            async with _gen_sem:
                try:
                    await make()
                except ProbeError as e:
                    raise HTTPException(422, f"cannot generate: {e}") from e
    return dest


def _cache_for(v: str | None, version: str) -> dict:
    return {"Cache-Control": IMMUTABLE if v == version else REVALIDATE, "ETag": f'"{version}"'}


def _check_width(w: int) -> int:
    if w not in generated.THUMB_WIDTHS:
        raise HTTPException(422, f"w must be one of {generated.THUMB_WIDTHS}")
    return w


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------

async def _scene(request: Request, scene_id: int):
    row = await request.app.state.pool.fetchrow(
        """SELECT s.id, s.duration, s.height, s.video_codec, s.audio_codec,
                  f.id AS file_id, f.path, f.oshash, f.missing_since
           FROM scenes s JOIN files f ON f.id = s.file_id WHERE s.id = $1""",
        scene_id,
    )
    if row is None:
        raise HTTPException(404, "scene not found")
    if row["missing_since"] is not None or not os.path.isfile(row["path"]):
        raise HTTPException(404, "scene file is missing")
    return row


@router.api_route("/scenes/{scene_id}/stream", methods=["GET", "HEAD"])
async def stream_direct(scene_id: int, request: Request):
    """The original file, with HTTP Range support (206 Partial Content) for instant seeking."""
    s = await _scene(request, scene_id)
    return VideoFileResponse(
        s["path"], media_type=streaming.mime_type(s["path"]),
        headers={"Cache-Control": "private, no-transform"},
    )


@router.get("/scenes/{scene_id}/stream.mp4")
async def stream_converted(
    scene_id: int, request: Request,
    mode: str = Query("transcode", pattern="^(remux|transcode)$"),
    start: float = Query(0, ge=0),
    height: int | None = Query(None),
):
    """Live fragmented MP4 for browsers that can't play the original. Seek with `start`."""
    s = await _scene(request, scene_id)
    if height is not None and height not in streaming.TRANSCODE_HEIGHTS:
        raise HTTPException(422, f"height must be one of {streaming.TRANSCODE_HEIGHTS}")
    if mode == "remux" and s["video_codec"] not in streaming.REMUXABLE_VIDEO:
        raise HTTPException(422, f"{s['video_codec']} cannot be remuxed; use mode=transcode")
    if s["duration"] and start >= s["duration"]:
        raise HTTPException(416, "start is past the end of the video")
    limiter: streaming.TranscodeLimiter = request.app.state.transcodes
    if not limiter.available():
        raise HTTPException(503, "too many concurrent transcodes", headers={"Retry-After": "2"})
    args = streaming.ffmpeg_args(s["path"], mode, start, height, s["video_codec"])
    return StreamingResponse(
        streaming.ffmpeg_stream(args, limiter), media_type="video/mp4",
        headers={"Cache-Control": "no-store", "X-Stream-Start": f"{start:.3f}"},
    )


@router.get("/scenes/{scene_id}/playback")
async def playback(scene_id: int, request: Request) -> dict:
    """Everything the player needs to pick a delivery mode."""
    s = await _scene(request, scene_id)
    base = f"/api/scenes/{scene_id}"
    version = generated.scene_version(s["oshash"], s["file_id"])
    sprite_img, sprite_vtt = generated.sprite_paths(get_settings().generated_dir, version)
    return {
        "modes": streaming.playback_modes(s["path"], s["video_codec"], s["audio_codec"]),
        "direct": {"url": f"{base}/stream", "mime_type": streaming.mime_type(s["path"])},
        "remux_url": f"{base}/stream.mp4?mode=remux",
        "transcode_url": f"{base}/stream.mp4?mode=transcode",
        "transcode_heights": [h for h in streaming.TRANSCODE_HEIGHTS if not s["height"] or h <= s["height"]],
        "video_codec": s["video_codec"],
        "audio_codec": s["audio_codec"],
        "duration": s["duration"],
        "screenshot_url": f"{base}/screenshot?v={version}",
        "sprite_vtt_url": f"{base}/sprite.vtt?v={version}" if sprite_vtt.exists() and sprite_img.exists() else None,
    }


@router.get("/scenes/{scene_id}/screenshot")
async def screenshot(scene_id: int, request: Request, w: int = 1280, v: str | None = None):
    _check_width(w)
    s = await _scene(request, scene_id)
    root = get_settings().generated_dir
    version = generated.scene_version(s["oshash"], s["file_id"])
    master = generated.screenshot_path(root, version)
    await _ensure(master, lambda: generated.make_screenshot(s["path"], s["duration"], master))
    out = master
    if w != generated.SCREENSHOT_WIDTH:
        out = generated.thumbnail_path(root, "scenes", version, w)
        await _ensure(out, lambda: asyncio.to_thread(generated.make_thumbnail, master, w, out))
    return FileResponse(out, media_type="image/webp", headers=_cache_for(v, version))


async def _sprite_file(scene_id: int, request: Request, v: str | None, which: int, media_type: str):
    s = await _scene(request, scene_id)
    version = generated.scene_version(s["oshash"], s["file_id"])
    path = generated.sprite_paths(get_settings().generated_dir, version)[which]
    if not path.exists():
        # Too slow to build inline; queue it and let the player do without for now.
        await jobs.enqueue(request.app.state.pool, "generate", {"scene_ids": [scene_id]},
                           dedupe_key=f"generate:scene:{scene_id}")
        raise HTTPException(404, "sprite not generated yet; queued")
    return FileResponse(path, media_type=media_type, headers=_cache_for(v, version))


@router.get("/scenes/{scene_id}/sprite.webp")
async def sprite_image(scene_id: int, request: Request, v: str | None = None):
    return await _sprite_file(scene_id, request, v, 0, "image/webp")


@router.get("/scenes/{scene_id}/sprite.vtt")
async def sprite_vtt(scene_id: int, request: Request, v: str | None = None):
    return await _sprite_file(scene_id, request, v, 1, "text/vtt")


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

async def _image(request: Request, image_id: int):
    row = await request.app.state.pool.fetchrow(
        """SELECT i.id, i.format, f.id AS file_id, f.path, f.mod_time,
                  coalesce(z.missing_since, f.missing_since) AS missing_since, z.path AS zip_path
           FROM images i JOIN files f ON f.id = i.file_id
           LEFT JOIN files z ON z.id = f.zip_file_id
           WHERE i.id = $1""",
        image_id,
    )
    if row is None:
        raise HTTPException(404, "image not found")
    on_disk = row["zip_path"] or row["path"]
    if row["missing_since"] is not None or not os.path.isfile(on_disk):
        raise HTTPException(404, "image file is missing")
    return row


@router.get("/images/{image_id}/thumbnail")
async def image_thumbnail(image_id: int, request: Request, w: int = 320, v: str | None = None):
    _check_width(w)
    img = await _image(request, image_id)
    version = generated.image_version(img["file_id"], img["mod_time"])
    out = generated.thumbnail_path(get_settings().generated_dir, "images", version, w)

    def make():
        with generated.open_image_source(img["path"], img["zip_path"]) as src:
            generated.make_thumbnail(src, w, out)

    await _ensure(out, lambda: asyncio.to_thread(make))
    return FileResponse(out, media_type="image/webp", headers=_cache_for(v, version))


@router.get("/images/{image_id}/file")
async def image_file(image_id: int, request: Request, v: str | None = None):
    """The original image, for the full-size lightbox view."""
    img = await _image(request, image_id)
    version = generated.image_version(img["file_id"], img["mod_time"])
    media_type = mimetypes.guess_type(img["path"])[0] or "application/octet-stream"
    headers = _cache_for(v, version)
    if img["zip_path"] is None:
        return FileResponse(img["path"], media_type=media_type, headers=headers)

    def read():
        with generated.open_image_source(img["path"], img["zip_path"]) as f:
            return f.read()

    return Response(await asyncio.to_thread(read), media_type=media_type, headers=headers)


# ---------------------------------------------------------------------------
# Batch generation
# ---------------------------------------------------------------------------

@router.post("/generate", status_code=202)
async def generate_missing(request: Request) -> dict:
    """Queue screenshot/sprite generation for every scene that lacks them."""
    job_id = await jobs.enqueue(request.app.state.pool, "generate", {"missing": True},
                                dedupe_key="generate:missing")
    return {"job_id": job_id}
