"""Video delivery: direct play decisions and live remux/transcode via ffmpeg.

Direct play (HTTP Range on the original file) is always preferred: zero CPU and
instant seeking. When the browser can't decode the container or codec, the player
falls back to a fragmented-MP4 stream produced by ffmpeg on the fly. Such a stream is
not byte-seekable, so the player seeks by re-requesting with `start=<seconds>`.
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable

log = logging.getLogger(__name__)

MIME_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".mkv": "video/x-matroska", ".ogv": "video/ogg",
    ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv",
    ".ts": "video/mp2t", ".m2ts": "video/mp2t", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".3gp": "video/3gpp",
}

# What mainstream browsers can play natively. The frontend makes the final call with
# HTMLMediaElement.canPlayType(); this is the server's best guess for choosing a default.
BROWSER_CONTAINERS = {".mp4", ".m4v", ".mov", ".webm", ".ogv"}
BROWSER_VIDEO = {"h264", "vp8", "vp9", "av1", "hevc"}
BROWSER_AUDIO = {None, "aac", "mp3", "opus", "vorbis", "flac"}
# Video codecs that can be copied into fragmented MP4 without re-encoding.
REMUXABLE_VIDEO = {"h264", "hevc", "av1", "vp9"}

TRANSCODE_HEIGHTS = (360, 480, 720, 1080)
CHUNK = 256 * 1024


def mime_type(path: str) -> str:
    return MIME_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def playback_modes(path: str, video_codec: str | None, audio_codec: str | None) -> list[str]:
    """Delivery modes in preference order."""
    modes = []
    ext = os.path.splitext(path)[1].lower()
    if ext in BROWSER_CONTAINERS and video_codec in BROWSER_VIDEO and audio_codec in BROWSER_AUDIO:
        modes.append("direct")
    if video_codec in REMUXABLE_VIDEO:
        modes.append("remux")
    modes.append("transcode")
    return modes


def ffmpeg_args(path: str, mode: str, start: float, height: int | None, video_codec: str | None) -> list[str]:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", path, "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    if mode == "remux":
        args += ["-c:v", "copy"]
        if video_codec == "hevc":
            args += ["-tag:v", "hvc1"]  # Safari/Chrome want hvc1, not hev1, in MP4
    else:
        vf = "format=yuv420p"  # also flattens 10-bit sources, which many browsers can't decode
        if height:
            vf = f"scale=-2:'min({height},ih)',{vf}"
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-vf", vf,
                 "-g", "60", "-profile:v", "high"]
    args += ["-c:a", "aac", "-b:a", "160k", "-ac", "2",
             "-movflags", "frag_keyframe+empty_moov+default_base_moof",
             "-f", "mp4", "pipe:1"]
    return args


class TranscodeLimiter:
    """Caps concurrent ffmpeg streams; an over-limit request gets 503 rather than
    queueing, so a burst of seeks can't pile up processes."""

    def __init__(self, limit: int):
        self.limit = limit
        self.active = 0

    def acquire(self) -> Callable[[], None] | None:
        """Take a slot now, or None when all are in use. Check and take happen with no
        await in between, so a burst of requests can't all get in. Returns the slot's
        release, which is safe to call more than once."""
        if self.active >= self.limit:
            return None
        self.active += 1
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self.active -= 1
        return release


async def ffmpeg_stream(args: list[str], release: Callable[[], None]) -> AsyncIterator[bytes]:
    """Yield ffmpeg's stdout, then give back the transcode slot taken for it. The
    process is killed as soon as the consumer goes away (client disconnect cancels this
    generator), so abandoned seeks don't leak CPU."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        while chunk := await proc.stdout.read(CHUNK):
            yield chunk
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        release()
