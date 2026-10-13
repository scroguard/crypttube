"""Generated content: screenshots, scrub sprites and WebP thumbnails.

Everything lives under settings.generated_dir, keyed by content version (a scene's
oshash, an image's file id + mtime), so URLs carrying that version can be cached
by the browser forever. Files are written to a temp name and atomically renamed,
so a reader never sees a half-written file and concurrent generators can't collide.
"""

import asyncio
import hashlib
import io
import math
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from app.media import ProbeError, background

THUMB_WIDTHS = (160, 320, 640, 1280)
# The sizes the grids ask for, made ahead of time so browsing never waits on a resize:
# scene cards and gallery covers use 640, image grids 320.
SCENE_GRID_WIDTH = 640
IMAGE_GRID_WIDTH = 320
COVER_WIDTH = 640
SCREENSHOT_WIDTH = 1280
SPRITE_COLUMNS = 9
SPRITE_MAX_FRAMES = 81
SPRITE_FRAME_WIDTH = 160
WEBP_QUALITY = 75


def _shard(root: Path, kind: str, key: str, suffix: str) -> Path:
    digest = hashlib.sha1(key.encode()).hexdigest()
    return root / kind / digest[:2] / f"{key}{suffix}"


def scene_version(oshash: str | None, file_id: int) -> str:
    return oshash or f"f{file_id}"


def image_version(file_id: int, mod_time) -> str:
    return f"{file_id}-{int(mod_time.timestamp() * 1_000_000):x}"


def url_version(version: str, rev: int) -> str:
    """The `v` query parameter clients see: the storage key plus the rebuild revision.
    (Storage paths use the bare key, so a rebuild overwrites in place.)"""
    return f"{version}.{rev}" if rev else version


def usable(path: Path) -> bool:
    """A generated file that exists, is non-empty and can be read. Anything else is
    treated as missing and regenerated."""
    try:
        return path.stat().st_size > 0 and os.access(path, os.R_OK)
    except OSError:
        return False


def screenshot_path(root: Path, version: str) -> Path:
    return _shard(root, "screenshots", version, ".webp")


def sprite_paths(root: Path, version: str) -> tuple[Path, Path]:
    return _shard(root, "sprites", version, ".webp"), _shard(root, "sprites", version, ".vtt")


def thumbnail_path(root: Path, kind: str, version: str, width: int) -> Path:
    return _shard(root, f"thumbs/{kind}", f"{version}-w{width}", ".webp")


def scene_asset_files(root: Path, version: str) -> list[Path]:
    return [screenshot_path(root, version), *sprite_paths(root, version),
            *(thumbnail_path(root, "scenes", version, w) for w in THUMB_WIDTHS)]


def display_path(root: Path, version: str) -> Path:
    """Full-size browser-friendly copy of an image in a format browsers can't show (HEIC...)."""
    return _shard(root, "display", version, ".webp")


def image_asset_files(root: Path, version: str) -> list[Path]:
    return [*(thumbnail_path(root, "images", version, w) for w in THUMB_WIDTHS), display_path(root, version)]


DISPLAY_MAX = 4096   # px on the long side; WebP can't go past 16383, and viewers don't need more


def make_display_copy(src, dest: Path) -> None:
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            if max(im.size) > DISPLAY_MAX:
                im.thumbnail((DISPLAY_MAX, DISPLAY_MAX), Image.Resampling.LANCZOS)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB")
            _atomic_write(dest, lambda f: im.save(f, "WEBP", quality=90, method=4))
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise ProbeError(f"cannot convert: {e}") from e


def remove_files(paths) -> None:
    """Delete generated files; a file that can't be removed is an error worth surfacing
    (it would otherwise keep being served, or keep failing to be replaced)."""
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except IsADirectoryError:
            shutil.rmtree(p)


def check_writable(root: Path) -> str | None:
    """None if generated content can be written; otherwise an explanation for the admin."""
    try:
        ensure_dir(root)
        fd, tmp = tempfile.mkstemp(dir=root, prefix=".write-test-")
        os.close(fd)
        os.unlink(tmp)
        return None
    except OSError as e:
        return (f"the generated-content volume ({root}) is not writable by the app "
                f"(running as uid {os.getuid()}): {e.strerror or e}")


# Everything rebuildable. `entities/` (uploaded performer/studio/tag images) is not.
REBUILDABLE_DIRS = ("screenshots", "sprites", "thumbs", "display")


def wipe_rebuildable(root: Path) -> None:
    for name in REBUILDABLE_DIRS:
        target = root / name
        if target.exists():
            shutil.rmtree(target)


# Generated content lives in a Docker volume that only this stack's containers mount.
# Files and folders are created group/world-accessible so that changing PUID later
# never leaves earlier files unreadable or folders unwritable.
FILE_MODE = 0o666
DIR_MODE = 0o777


def ensure_dir(path: Path) -> None:
    if path.is_dir():
        return
    ensure_dir(path.parent)
    try:
        path.mkdir(mode=DIR_MODE)
        os.chmod(path, DIR_MODE)  # mkdir's mode is filtered by the umask
    except FileExistsError:
        pass


def _atomic_write(dest: Path, write) -> None:
    ensure_dir(dest.parent)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")  # created 0600
    try:
        with os.fdopen(fd, "wb") as f:
            write(f)
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, dest)
    except BaseException:
        os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def open_image_source(path: str, zip_path: str | None = None):
    """A readable binary file object for a plain file or a zip member."""
    if zip_path is None:
        return open(path, "rb")
    zf = zipfile.ZipFile(zip_path)
    try:
        return io.BytesIO(zf.read(path))
    finally:
        zf.close()


def make_thumbnail(src, width: int, dest: Path) -> None:
    """Downscale (never upscale) to `width`, honouring EXIF orientation, save as WebP."""
    try:
        with Image.open(src) as im:
            # For JPEGs, decode at reduced scale: much faster than full decode + resize.
            im.draft("RGB", (width, width))
            im = ImageOps.exif_transpose(im)
            if im.width > width:
                im.thumbnail((width, im.height * width // im.width + 1), Image.Resampling.LANCZOS)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB")
            _atomic_write(dest, lambda f: im.save(f, "WEBP", quality=WEBP_QUALITY, method=4))
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise ProbeError(f"cannot thumbnail: {e}") from e


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------

async def _run_ffmpeg(args: list[str], timeout: float = 120) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *background(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *args]),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise ProbeError(err.decode(errors="replace").strip()[-500:] or "ffmpeg failed")
    return out


async def _frame_at(path: str, seconds: float, width: int, *, fast: bool) -> Image.Image | None:
    """Decode a single frame as a PIL image. `fast` snaps to the nearest keyframe."""
    # -noaccurate_seek emits the keyframe at or before the seek point: one decoded
    # frame instead of decoding forward to the exact timestamp.
    seek = ["-noaccurate_seek"] if fast else []
    # One decoder thread: by default ffmpeg starts a thread per core for every process,
    # and several of these run at once.
    out = await _run_ffmpeg([
        "-threads", "1", *seek, "-ss", f"{seconds:.3f}", "-i", path, "-frames:v", "1", "-an", "-sn",
        "-vf", f"scale='min({width},iw)':-2", "-f", "image2pipe", "-c:v", "png", "-",
    ])
    if not out:
        return None
    im = Image.open(io.BytesIO(out))
    im.load()
    return im.convert("RGB")


async def make_screenshot(path: str, duration: float | None, dest: Path) -> None:
    t = (duration or 0) * 0.2
    frame = await _frame_at(path, t, SCREENSHOT_WIDTH, fast=False)
    if frame is None and t:
        frame = await _frame_at(path, 0, SCREENSHOT_WIDTH, fast=False)
    if frame is None:
        raise ProbeError("no frame decoded")
    await asyncio.to_thread(
        _atomic_write, dest, lambda f: frame.save(f, "WEBP", quality=80, method=4)
    )


def _vtt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


@dataclass(frozen=True)
class SpriteLayout:
    frames: int
    columns: int
    rows: int


def sprite_layout(duration: float) -> SpriteLayout:
    frames = max(1, min(SPRITE_MAX_FRAMES, int(duration)))
    columns = min(SPRITE_COLUMNS, frames)
    return SpriteLayout(frames, columns, math.ceil(frames / columns))


async def make_sprite(path: str, duration: float, version: str, dest_img: Path, dest_vtt: Path,
                      concurrency: int = 2) -> None:
    """A grid of evenly spaced keyframes plus a WebVTT file mapping time ranges to tiles
    (the `#xywh=` media-fragment convention understood by most web players)."""
    if not duration or duration <= 0:
        raise ProbeError("unknown duration")
    layout = sprite_layout(duration)
    step = duration / layout.frames
    sem = asyncio.Semaphore(concurrency)

    async def grab(i: int):
        async with sem:
            try:
                return await _frame_at(path, (i + 0.5) * step, SPRITE_FRAME_WIDTH, fast=True)
            except ProbeError:
                return None

    frames = await asyncio.gather(*(grab(i) for i in range(layout.frames)))
    first = next((f for f in frames if f is not None), None)
    if first is None:
        raise ProbeError("no frames decoded for sprite")
    w, h = first.size

    def build() -> str:
        sheet = Image.new("RGB", (w * layout.columns, h * layout.rows))
        cues = ["WEBVTT", ""]
        for i, frame in enumerate(frames):
            x, y = (i % layout.columns) * w, (i // layout.columns) * h
            if frame is not None:
                sheet.paste(frame.resize((w, h)) if frame.size != (w, h) else frame, (x, y))
            cues += [f"{_vtt_time(i * step)} --> {_vtt_time((i + 1) * step)}",
                     f"sprite.webp?v={version}#xywh={x},{y},{w},{h}", ""]
        _atomic_write(dest_img, lambda f: sheet.save(f, "WEBP", quality=70, method=4))
        return "\n".join(cues)

    vtt = await asyncio.to_thread(build)
    await asyncio.to_thread(_atomic_write, dest_vtt, lambda f: f.write(vtt.encode()))


async def generate_scene_assets(root: Path, path: str, duration: float | None, version: str,
                                *, url_ver: str | None = None, force: bool = False) -> list[str]:
    """Create whatever is missing or unusable for one scene; with `force`, discard and
    rebuild everything. `url_ver` is the version clients will request the sprite sheet
    with (written into the VTT). Returns the names of what was made."""
    made = []
    if force:
        await asyncio.to_thread(remove_files, scene_asset_files(root, version))
    shot = screenshot_path(root, version)
    if not usable(shot):
        await make_screenshot(path, duration, shot)
        made.append("screenshot")
    sprite_img, sprite_vtt = sprite_paths(root, version)
    if duration and not (usable(sprite_img) and usable(sprite_vtt)):
        await make_sprite(path, duration, url_ver or version, sprite_img, sprite_vtt)
        made.append("sprite")
    return made
