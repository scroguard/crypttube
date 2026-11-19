"""ffprobe and ffmpeg only ever read the file they're given: no other files or network addresses."""

import socket
import subprocess
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
        with pytest.raises(media.ProbeError):
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


def test_input_options():
    assert media.input_options("/media/a.mp4")[:2] == ["-protocol_whitelist", "file"]
    assert media.input_options("http://127.0.0.1:41234/tok")[:2] == ["-protocol_whitelist", "http,tcp"]
    assert media.input_options("http://10.0.0.5:80/x")[:2] == ["-protocol_whitelist", "file"]   # only the loopback stream
    assert "-format_whitelist" in media.input_options("/media/a.mp4")


async def test_a_format_outside_the_list_is_refused(tmp_path):
    """Proves the options are applied: ffmpeg would read this MP3 without them (renamed .mp4, which the library
    would list as a video); with them it's refused, and the scan records it as unreadable rather than failing."""
    p = tmp_path / "song.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", "-f", "mp3", str(p)], check=True)
    plain = subprocess.run(["ffprobe", "-v", "error", "-show_format", str(p)], capture_output=True, text=True)
    assert plain.returncode == 0 and "format_name=mp3" in plain.stdout          # readable without the options
    with pytest.raises(media.ProbeError, match="whitelist"):
        await media.ffprobe(str(p))


def _zip(path, members):
    import zipfile
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)


def _png(size=(8, 8)) -> bytes:
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", size, (200, 10, 10)).save(b, "PNG")
    return b.getvalue()


def test_archive_members_over_the_limits_are_not_read(tmp_path, monkeypatch):
    """A member that unpacks to more than the limit is counted unreadable, not unpacked."""
    from app import media
    monkeypatch.setattr(media, "ARCHIVE_MAX_MEMBER", 1024 * 1024)
    z = tmp_path / "a.zip"
    _zip(z, [("ok.png", _png()), ("huge.png", b"\0" * (20 * 1024 * 1024))])   # 20 MB of zeros: ~1000:1
    members, bad = media.read_archive(str(z))
    assert [m.path for m in members] == ["ok.png"] and bad == 1


def test_opening_a_member_streams_it_and_refuses_one_over_the_limits(tmp_path, monkeypatch):
    import pytest
    from app import generated, media
    monkeypatch.setattr(media, "ARCHIVE_MAX_MEMBER", 1024 * 1024)
    z = tmp_path / "a.zip"
    _zip(z, [("ok.png", _png()), ("huge.png", b"\0" * (20 * 1024 * 1024))])
    with generated.open_image_source("ok.png", str(z)) as f:
        assert f.read() == _png() and f.seekable()
    with pytest.raises(media.ProbeError):
        generated.open_image_source("huge.png", str(z))


def test_an_archive_with_too_many_entries_isnt_read(tmp_path, monkeypatch):
    import pytest
    from app import media
    monkeypatch.setattr(media, "ARCHIVE_MAX_MEMBERS", 3)
    z = tmp_path / "a.zip"
    _zip(z, [(f"{i}.png", _png()) for i in range(4)])
    with pytest.raises(media.ProbeError, match="entries"):
        media.read_archive(str(z))
