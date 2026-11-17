"""ffprobe and ffmpeg only ever read the file they're given: no other files or network addresses."""

import socket
import threading

import pytest

from app import crypto, generated, media
from tests.test_crypto import KEY


@pytest.fixture
def listener():
    """A local TCP port that records whether anything connected to it."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    srv.settimeout(0.2)
    hits = []
    stop = threading.Event()

    def run():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                hits.append(1)
                conn.close()
            except OSError:
                pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    yield srv.getsockname()[1], hits
    stop.set()
    t.join(1)
    srv.close()


def not_media(tmp_path, port):
    """A text file with a video extension that refers to another address."""
    p = tmp_path / "clip.mp4"
    p.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:{port}/a.ts\n#EXT-X-ENDLIST\n")
    return p


@pytest.mark.parametrize("encrypted", [False, True], ids=["plain", "encrypted"])
async def test_probe_reads_only_the_file(tmp_path, listener, encrypted):
    port, hits = listener
    p = not_media(tmp_path, port)
    if encrypted:
        crypto.set_key(KEY)
        p.write_bytes(crypto.encrypt_bytes(p.read_bytes()))
    try:
        with pytest.raises(media.ProbeError):
            await media.ffprobe(str(p), timeout=20)
        with pytest.raises(Exception):
            await generated.make_screenshot(str(p), 2.0, tmp_path / "shot.webp")
    finally:
        crypto.set_key(None)
    assert hits == []


async def test_real_media_still_works_plain_and_encrypted(tmp_path, sample_media):
    data = (sample_media / "h264.mp4").read_bytes()
    p = tmp_path / "v.mp4"
    p.write_bytes(data)
    assert (await media.ffprobe(str(p)))["streams"]
    crypto.set_key(KEY)
    try:
        p.write_bytes(crypto.encrypt_bytes(data))
        assert (await media.ffprobe(str(p)))["streams"]
    finally:
        crypto.set_key(None)
