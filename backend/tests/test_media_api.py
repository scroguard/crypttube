import asyncio
import io
import subprocess
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from app import generated, worker
from app.config import get_settings
from app.jobs import JobContext
from app.scanner import Scanner


@pytest.fixture
async def indexed(app, clean, library_dir):
    """A scanned library; returns lookup helpers for its rows."""
    pool = app.state.pool
    # an EXIF-rotated JPEG: stored 80x40, displayed 40x80
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90 CW
    Image.new("RGB", (80, 40), "white").save(library_dir / "pics" / "rotated.jpg", exif=exif)
    lid = await pool.fetchval("INSERT INTO library_paths (path) VALUES ($1) RETURNING id", str(library_dir))
    await Scanner(pool, get_settings().generated_dir, settle_seconds=0).scan(lid)
    scenes = {r["video_codec"]: r["id"] for r in await pool.fetch("SELECT id, video_codec FROM scenes")}
    images = {r["path"]: r["id"] for r in await pool.fetch(
        "SELECT i.id, f.path FROM images i JOIN files f ON f.id = i.file_id")}
    return {"root": library_dir, "scenes": scenes, "images": images}


def _image_id(indexed, suffix):
    return next(i for p, i in indexed["images"].items() if p.endswith(suffix))


# --- direct streaming / HTTP Range -----------------------------------------------

async def test_stream_full_and_ranges(client, indexed):
    sid = indexed["scenes"]["h264"]
    data = (indexed["root"] / "videos" / "h264.mp4").read_bytes()
    size = len(data)

    r = await client.get(f"/api/scenes/{sid}/stream")
    assert r.status_code == 200
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "video/mp4"
    assert r.content == data

    r = await client.get(f"/api/scenes/{sid}/stream", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 100-199/{size}"
    assert r.headers["content-length"] == "100"
    assert r.content == data[100:200]

    r = await client.get(f"/api/scenes/{sid}/stream", headers={"Range": f"bytes={size - 50}-"})
    assert r.status_code == 206 and r.content == data[-50:]

    r = await client.get(f"/api/scenes/{sid}/stream", headers={"Range": "bytes=-10"})
    assert r.status_code == 206 and r.content == data[-10:]

    r = await client.get(f"/api/scenes/{sid}/stream", headers={"Range": f"bytes={size + 10}-"})
    assert r.status_code == 416


async def test_stream_head_and_if_range(client, indexed):
    sid = indexed["scenes"]["h264"]
    head = await client.head(f"/api/scenes/{sid}/stream")
    assert head.status_code == 200 and head.content == b""
    etag = head.headers["etag"]
    r = await client.get(f"/api/scenes/{sid}/stream", headers={"Range": "bytes=0-9", "If-Range": etag})
    assert r.status_code == 206
    r = await client.get(f"/api/scenes/{sid}/stream", headers={"Range": "bytes=0-9", "If-Range": '"stale"'})
    assert r.status_code == 200  # validator mismatch: send the whole (changed) file


async def test_missing_scene_file_is_404(app, client, indexed):
    sid = indexed["scenes"]["vp9"]
    (indexed["root"] / "videos" / "vp9.webm").unlink()
    assert (await client.get(f"/api/scenes/{sid}/stream")).status_code == 404
    assert (await client.get("/api/scenes/999999/stream")).status_code == 404


# --- playback decision ----------------------------------------------------------

async def test_playback_modes(client, indexed):
    mp4 = (await client.get(f"/api/scenes/{indexed['scenes']['h264']}/playback")).json()
    assert mp4["modes"] == ["direct", "remux", "transcode"]
    assert mp4["direct"]["mime_type"] == "video/mp4"
    mkv = (await client.get(f"/api/scenes/{indexed['scenes']['hevc']}/playback")).json()
    assert mkv["modes"] == ["remux", "transcode"]  # MKV container isn't reliably playable
    assert mkv["transcode_heights"] == []  # source is 240p; never upscale
    webm = (await client.get(f"/api/scenes/{indexed['scenes']['vp9']}/playback")).json()
    assert webm["modes"][0] == "direct"


# --- remux / transcode ----------------------------------------------------------

def _probe_bytes(tmp_path: Path, data: bytes) -> str:
    f = tmp_path / "out.mp4"
    f.write_bytes(data)
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,pix_fmt",
         "-of", "csv=p=0", str(f)], capture_output=True, text=True, check=True).stdout.strip()


async def test_remux_hevc_to_fmp4(client, indexed, tmp_path):
    r = await client.get(f"/api/scenes/{indexed['scenes']['hevc']}/stream.mp4", params={"mode": "remux"})
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert _probe_bytes(tmp_path, r.content).startswith("hevc")


async def test_transcode_to_h264_with_start(client, indexed, tmp_path):
    r = await client.get(f"/api/scenes/{indexed['scenes']['vp9']}/stream.mp4",
                         params={"mode": "transcode", "start": 0.5})
    assert r.status_code == 200
    assert r.headers["x-stream-start"] == "0.500"
    assert _probe_bytes(tmp_path, r.content) == "h264,yuv420p"


async def test_transcode_validation_and_limit(app, client, indexed):
    vp9 = indexed["scenes"]["vp9"]
    assert (await client.get(f"/api/scenes/{vp9}/stream.mp4", params={"height": 123})).status_code == 422
    assert (await client.get(f"/api/scenes/{vp9}/stream.mp4", params={"start": 999})).status_code == 416
    limiter = app.state.transcodes
    limiter.active = limiter.limit
    try:
        r = await client.get(f"/api/scenes/{vp9}/stream.mp4")
        assert r.status_code == 503 and r.headers["retry-after"] == "2"
    finally:
        limiter.active = 0


async def test_transcode_process_killed_when_consumer_stops(indexed):
    from app import streaming
    limiter = streaming.TranscodeLimiter(1)
    path = str(indexed["root"] / "videos" / "vp9.webm")
    gen = streaming.ffmpeg_stream(streaming.ffmpeg_args(path, "transcode", 0, None, "vp9"), limiter)
    await gen.__anext__()
    assert limiter.active == 1
    await gen.aclose()  # what a client disconnect does
    assert limiter.active == 0


# --- screenshots & thumbnails ---------------------------------------------------

async def test_screenshot_on_demand_and_caching(client, indexed):
    sid = indexed["scenes"]["h264"]
    info = (await client.get(f"/api/scenes/{sid}/playback")).json()
    version = info["screenshot_url"].split("v=")[1]

    r = await client.get(info["screenshot_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert "immutable" in r.headers["cache-control"]
    assert Image.open(io.BytesIO(r.content)).size == (320, 240)  # never upscaled

    r = await client.get(f"/api/scenes/{sid}/screenshot", params={"w": 160})
    assert Image.open(io.BytesIO(r.content)).width == 160
    assert r.headers["cache-control"] == "private, no-cache"  # no/old version: revalidate
    assert generated.thumbnail_path(get_settings().generated_dir, "scenes", version, 160).exists()

    assert (await client.get(f"/api/scenes/{sid}/screenshot", params={"w": 333})).status_code == 422


async def test_concurrent_requests_generate_once(client, indexed, monkeypatch):
    calls = 0
    real = generated.make_screenshot

    async def counting(*a):
        nonlocal calls
        calls += 1
        await real(*a)

    monkeypatch.setattr(generated, "make_screenshot", counting)
    sid = indexed["scenes"]["vp9"]
    rs = await asyncio.gather(*(client.get(f"/api/scenes/{sid}/screenshot") for _ in range(5)))
    assert all(r.status_code == 200 for r in rs)
    assert calls == 1


async def test_image_thumbnail_sizes_and_orientation(client, indexed):
    img = _image_id(indexed, "pics/rotated.jpg")
    r = await client.get(f"/api/images/{img}/thumbnail", params={"w": 160})
    assert r.headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(r.content)).size == (40, 80)  # EXIF rotation applied, not upscaled

    small = _image_id(indexed, "pics/img2.jpg")
    im = Image.open(io.BytesIO((await client.get(f"/api/images/{small}/thumbnail", params={"w": 160})).content))
    assert im.size == (64, 48)


async def test_large_image_is_downscaled(client, app, indexed):
    big = indexed["root"] / "pics" / "big.jpg"
    Image.new("RGB", (3000, 2000), "purple").save(big)
    lid = await app.state.pool.fetchval("SELECT id FROM library_paths")
    await Scanner(app.state.pool, get_settings().generated_dir, settle_seconds=0).scan(lid)
    img = await app.state.pool.fetchval(
        "SELECT i.id FROM images i JOIN files f ON f.id = i.file_id WHERE f.path = $1", str(big))
    r = await client.get(f"/api/images/{img}/thumbnail", params={"w": 640})
    assert Image.open(io.BytesIO(r.content)).size == (640, 427)
    assert len(r.content) < 20_000


async def test_zip_member_thumbnail_and_original(client, indexed):
    member = indexed["images"]["a.jpg"]
    r = await client.get(f"/api/images/{member}/thumbnail")
    assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).format == "WEBP"
    r = await client.get(f"/api/images/{member}/file")
    assert r.headers["content-type"] == "image/jpeg"
    with zipfile.ZipFile(indexed["root"] / "archive.zip") as zf:
        assert r.content == zf.read("a.jpg")


async def test_plain_image_original(client, indexed):
    img = _image_id(indexed, "pics/img10.jpg")
    r = await client.get(f"/api/images/{img}/file")
    assert r.content == (indexed["root"] / "pics" / "img10.jpg").read_bytes()


# --- sprites & generation job ---------------------------------------------------

async def test_sprite_missing_queues_generation(app, client, indexed):
    sid = indexed["scenes"]["h264"]
    r = await client.get(f"/api/scenes/{sid}/sprite.vtt")
    assert r.status_code == 404
    job = await app.state.pool.fetchrow("SELECT kind, payload FROM jobs")
    assert (job["kind"], job["payload"]) == ("generate", {"scene_ids": [sid]})


async def test_generate_job_builds_sprites(app, client, indexed):
    pool = app.state.pool
    job_id = await pool.fetchval(
        "INSERT INTO jobs (kind, payload, status) VALUES ('generate', '{\"missing\": true}', 'running') RETURNING id")
    settings = get_settings()
    msg = await worker.handle_generate(pool, settings, JobContext(pool, job_id), {"missing": True})
    assert msg == "3 generated"
    # idempotent
    assert await worker.handle_generate(pool, settings, JobContext(pool, job_id), {"missing": True}) == "0 generated"

    sid = indexed["scenes"]["h264"]
    info = (await client.get(f"/api/scenes/{sid}/playback")).json()
    assert info["sprite_vtt_url"]
    vtt = (await client.get(info["sprite_vtt_url"])).text
    lines = vtt.splitlines()
    assert lines[0] == "WEBVTT"
    assert lines[2] == "00:00:00.000 --> 00:00:01.000"  # a 1s clip -> a single tile
    assert lines[3].startswith("sprite.webp?v=") and lines[3].endswith("#xywh=0,0,160,120")

    ref = lines[3].split("#")[0]
    r = await client.get(f"/api/scenes/{sid}/{ref}")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert Image.open(io.BytesIO(r.content)).size == (160, 120)


def test_sprite_layout():
    assert generated.sprite_layout(0.5) == generated.SpriteLayout(1, 1, 1)
    assert generated.sprite_layout(30) == generated.SpriteLayout(30, 9, 4)
    assert generated.sprite_layout(7200) == generated.SpriteLayout(81, 9, 9)


async def test_vtt_times():
    assert generated._vtt_time(3725.5) == "01:02:05.500"


async def test_scan_queues_generation_for_new_scenes_only(app, indexed):
    # `indexed` scanned directly (not via the worker), so no generate job exists yet.
    pool = app.state.pool
    lid = await pool.fetchval("SELECT id FROM library_paths")
    await pool.execute("INSERT INTO jobs (kind, payload) VALUES ('scan', $1)", {"library_id": lid})
    await worker.run_one(pool, get_settings().model_copy(update={"scan_settle_seconds": 0}))
    assert await pool.fetchval("SELECT count(*) FROM jobs WHERE kind = 'generate'") == 0  # nothing new
