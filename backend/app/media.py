"""File classification and probing (ffprobe for video, Pillow for images)."""

import asyncio
import json
import os
import struct
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from PIL import Image

VIDEO_EXTS = frozenset(
    ".mp4 .m4v .mkv .webm .mov .avi .wmv .flv .mpg .mpeg .ts .m2ts .3gp .ogv".split()
)
IMAGE_EXTS = frozenset(".jpg .jpeg .png .webp .gif .bmp .tif .tiff .avif".split())
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


async def ffprobe(path: str, timeout: float = 60) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path,
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
