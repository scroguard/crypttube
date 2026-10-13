"""File classification and probing (ffprobe for video, Pillow for images)."""

import asyncio
import functools
import json
import os
import shutil
import struct
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from PIL import Image
from pillow_heif import register_heif_opener

register_heif_opener()   # lets Pillow open HEIC/HEIF (iPhone photos) everywhere it opens images

VIDEO_EXTS = frozenset(
    ".mp4 .m4v .mkv .webm .mov .avi .wmv .flv .mpg .mpeg .ts .m2ts .3gp .ogv".split()
)
IMAGE_EXTS = frozenset(".jpg .jpeg .png .webp .gif .bmp .tif .tiff .avif .heic .heif".split())
# Formats most browsers can't display: the image viewer gets a converted copy of these.
NOT_FOR_BROWSERS = frozenset(".heic .heif .tif .tiff .bmp".split())
ARCHIVE_EXTS = frozenset(".zip .cbz".split())

OSHASH_CHUNK = 64 * 1024


def classify(name: str) -> str | None:
    ext = os.path.splitext(name)[1].lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in ARCHIVE_EXTS:
        return "archive"
    return None


def oshash(path: str) -> str:
    """OpenSubtitles-style hash: file size + 64-bit word sums of the first and last 64 KiB.

    Reads at most 128 KiB regardless of file size, so it is cheap even for huge videos,
    and it survives renames/moves, which is what we use it for.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(OSHASH_CHUNK)
        f.seek(max(0, size - OSHASH_CHUNK))
        tail = f.read(OSHASH_CHUNK)
    total = size
    for chunk in (head, tail):
        chunk += b"\0" * (-len(chunk) % 8)
        for (word,) in struct.iter_unpack("<Q", chunk):
            total = (total + word) & 0xFFFFFFFFFFFFFFFF
    return f"{total:016x}"


class ProbeError(Exception):
    pass


MP4_EXTS = frozenset(".mp4 .m4v .mov".split())
MAX_MOOV = 256 * 1024 * 1024
CONTAINERS = (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts", b"mvex")


@dataclass(frozen=True)
class Protection:
    """DRM markings found in an MP4's header.

    `pssh_types`: file offsets of the type field of each DRM init box (pssh). Files saved
    from streaming sites often keep these although the content isn't encrypted, and
    Firefox (and iOS) then refuse to play them. Served with those boxes relabelled as
    'free' (padding, same size), the file plays everywhere.
    `encrypted`: the tracks themselves are encrypted (encv/enca sample entries); no
    browser can play those without the keys."""
    pssh_types: tuple[int, ...] = ()
    encrypted: bool = False


def _read_protection(path: str) -> Protection:
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        off = 0
        while off + 8 <= size:
            f.seek(off)
            box, kind = struct.unpack(">I4s", f.read(8))
            header = 8
            if box == 1:
                box, header = struct.unpack(">Q", f.read(8))[0], 16
            elif box == 0:
                box = size - off
            if box < header:
                return Protection()                  # not a well-formed MP4 from here on
            if kind == b"moov":
                if box > MAX_MOOV:
                    return Protection()
                f.seek(off)
                moov = f.read(box)
                return Protection(tuple(off + o for o in _pssh_in(moov, 0, len(moov))),
                                  any(t in moov for t in (b"encv", b"enca")))
            off += box
    return Protection()


def _pssh_in(buf: bytes, start: int, end: int) -> list[int]:
    """Offsets (within buf) of the type field of pssh boxes in buf[start:end], looking
    inside the container boxes where they can appear."""
    found, off = [], start
    while off + 8 <= end:
        box, kind = struct.unpack(">I4s", buf[off:off + 8])
        header = 8
        if box == 1 and off + 16 <= end:
            box, header = struct.unpack(">Q", buf[off + 8:off + 16])[0], 16
        if box < header or off + box > end:
            break
        if kind == b"pssh":
            found.append(off + 4)
        elif kind in CONTAINERS:
            found += _pssh_in(buf, off + header, off + box)
        off += box
    return found


@functools.lru_cache(maxsize=4096)
def _protection_cached(path: str, size: int, mtime_ns: int) -> Protection:
    try:
        return _read_protection(path)
    except (OSError, struct.error):
        return Protection()


def mp4_protection(path: str) -> Protection:
    """Reads only box headers and the moov box; cached per file version."""
    if os.path.splitext(path)[1].lower() not in MP4_EXTS:
        return Protection()
    try:
        st = os.stat(path)
    except OSError:
        return Protection()
    return _protection_cached(path, st.st_size, st.st_mtime_ns)


@functools.cache
def _background_prefix() -> tuple[str, ...]:
    prefix: list[str] = []
    if shutil.which("nice"):
        prefix += ["nice", "-n", "15"]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c", "3"]   # idle I/O class (honoured by BFQ/CFQ; harmless otherwise)
    return tuple(prefix)


def background(cmd: list[str]) -> list[str]:
    """Run a helper process (ffprobe, ffmpeg for previews) at the lowest CPU and I/O
    priority, so indexing never competes with playback or anything else on the machine."""
    return [*_background_prefix(), *cmd]


async def ffprobe(path: str, timeout: float = 60) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *background(["ffprobe", "-v", "error", "-threads", "1", "-print_format", "json",
                     "-show_format", "-show_streams", path]),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ProbeError(f"ffprobe timed out after {timeout}s")
    if proc.returncode != 0:
        raise ProbeError(err.decode(errors="replace").strip() or f"ffprobe exited {proc.returncode}")
    return json.loads(out)


def _rate(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    num, _, den = value.partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_video_probe(data: dict) -> dict:
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        raise ProbeError("no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    bitrate = _float(fmt.get("bit_rate"))
    return {
        "duration": _float(fmt.get("duration")) or _float(video.get("duration")),
        "width": video.get("width"),
        "height": video.get("height"),
        "framerate": _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate")),
        "bitrate": int(bitrate) if bitrate else None,
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if audio else None,
        "container": fmt.get("format_name"),
    }


@dataclass(frozen=True)
class ImageInfo:
    width: int
    height: int
    format: str


def probe_image(fp) -> ImageInfo:
    """Read dimensions from the image header only; fp is a path or a file object."""
    try:
        with Image.open(fp) as im:
            return ImageInfo(im.width, im.height, (im.format or "").lower())
    except Exception as e:  # Pillow raises a variety of types for bad input
        raise ProbeError(f"unreadable image: {e}") from e


@dataclass(frozen=True)
class ArchiveMember:
    path: str
    size: int
    info: ImageInfo


def read_archive(path: str) -> tuple[list[ArchiveMember], int]:
    """List image members of a zip archive. Returns (members, unreadable_count)."""
    members, bad = [], 0
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ProbeError(f"bad archive: {e}") from e
    with zf:
        for zi in zf.infolist():
            name = zi.filename
            parts = PurePosixPath(name).parts
            if zi.is_dir() or any(p.startswith(".") or p == "__MACOSX" for p in parts):
                continue
            if classify(name) != "image":
                continue
            try:
                with zf.open(zi) as fp:
                    members.append(ArchiveMember(name, zi.file_size, probe_image(fp)))
            except ProbeError:
                bad += 1
    return members, bad
