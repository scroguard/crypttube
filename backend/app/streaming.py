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

from starlette.responses import StreamingResponse

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


# Live transcodes can use a GPU's H.264 encoder (HW_ACCEL): "vaapi" for AMD and Intel,
# "nvenc" for NVIDIA. Decoding and scaling stay on the CPU, so any source codec works;
# the encode is the expensive part. Set at startup by detect_hw_accel().
_accel = "none"
_device = "/dev/dri/renderD128"


def hw_accel() -> str:
    return _accel


def _encoder(accel: str, height: int | None) -> tuple[list[str], list[str]]:
    """(options before the input, video encode options) for one of the encoders."""
    scale = f"scale=-2:'min({height},ih)'," if height else ""
    # nv12 and yuv420p also flatten 10-bit sources, which many browsers can't decode
    if accel == "vaapi":
        pre = ["-vaapi_device", _device]
        video = ["-vf", f"{scale}format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "23"]
    elif accel == "nvenc":
        pre = []
        video = ["-vf", f"{scale}format=yuv420p", "-c:v", "h264_nvenc", "-preset", "p4",
                 "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    else:
        pre = []
        video = ["-vf", f"{scale}format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    return pre, [*video, "-g", "60", "-profile:v", "high"]


async def detect_hw_accel(accel: str, device: str) -> str:
    """Turn on the configured GPU encoder if a test encode works; otherwise log why and
    stay on the CPU, so a missing or misconfigured GPU never breaks playback."""
    global _accel, _device
    _accel, _device = "none", device
    if accel == "none":
        return _accel
    pre, video = _encoder(accel, 144)
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *pre,
            "-f", "lavfi", "-i", "testsrc2=size=256x144:rate=30:duration=0.5", *video, "-f", "null", "-"]
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(30):
            _, err = await proc.communicate()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        err = b"the test encode timed out"
    if proc.returncode != 0:
        log.warning("hardware encoding (%s) unavailable, transcoding on the CPU: %s",
                    accel, err.decode(errors="replace").strip()[-500:])
        return _accel
    _accel = accel
    log.info("hardware encoding on: %s", accel)
    return _accel


def ffmpeg_args(path: str, mode: str, start: float, height: int | None, video_codec: str | None,
                accel: str | None = None) -> list[str]:
    """`accel` overrides the detected encoder (None = use it)."""
    pre, video = _encoder(_accel if accel is None else accel, height) if mode != "remux" else ([], [])
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *pre]
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", path, "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    if mode == "remux":
        args += ["-c:v", "copy"]
        if video_codec == "hevc":
            args += ["-tag:v", "hvc1"]  # Safari/Chrome want hvc1, not hev1, in MP4
    else:
        args += video
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


class TranscodeResponse(StreamingResponse):
    """A transcode stream that gives back its slot only once ffmpeg is gone: when the
    response ends for any reason (a viewer who disconnects mid-chunk included), the body
    is closed, which stops ffmpeg, and then the slot is released."""

    def __init__(self, body: AsyncIterator[bytes], release: Callable[[], None], **kwargs):
        super().__init__(body, **kwargs)
        self.release = release

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.body_iterator.aclose()
            finally:
                self.release()


async def ffmpeg_stream(args: list[str], release: Callable[[], None],
                        fallback: list[str] | None = None) -> AsyncIterator[bytes]:
    """Yield ffmpeg's stdout, then give back the transcode slot taken for it. The
    process is killed as soon as the consumer goes away (client disconnect cancels this
    generator), so abandoned seeks don't leak CPU. If ffmpeg fails before producing
    anything (a GPU encoder that can't take this video), `fallback` is run instead."""
    proc = None
    try:
        for attempt in (args, fallback):
            if attempt is None:
                break
            proc = await asyncio.create_subprocess_exec(
                *attempt, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            produced = False
            while chunk := await proc.stdout.read(CHUNK):
                produced = True
                yield chunk
            if produced or await proc.wait() == 0:
                break
            if fallback is not None and attempt is args:
                log.warning("GPU transcode of %s failed; retrying on the CPU", args[args.index("-i") + 1])
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        release()
