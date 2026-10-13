"""Encryption at rest of users' uploads (app.crypto) and its use across the app."""

import base64
import io
import os
import secrets
from pathlib import Path

import pytest
from PIL import Image

from app import crypto, generated, media, worker
from app.config import get_settings
from app.jobs import JobContext
from tests.test_user_libraries import drain, people, upload  # noqa: F401  (fixtures)

KEY = secrets.token_bytes(32)


@pytest.fixture
def key():
    crypto.set_key(KEY)
    yield KEY
    crypto.set_key(None)


# --- the format ------------------------------------------------------------------------

@pytest.mark.parametrize("size", [0, 1, crypto.CHUNK - 1, crypto.CHUNK, crypto.CHUNK + 1, 3 * crypto.CHUNK + 12345])
def test_round_trip_and_random_access(tmp_path, key, size):
    data = secrets.token_bytes(size)
    path = tmp_path / "f.bin"
    path.write_bytes(crypto.encrypt_bytes(data))
    assert crypto.is_encrypted(path)
    if size >= 32:
        assert data[:32] not in path.read_bytes()
    assert crypto.plain_size(path) == size and crypto.read_all(path) == data
    with crypto.EncryptedReader(path) as r:
        for start, length in ((0, 10), (size // 2, 5000), (max(0, size - 7), 50), (crypto.CHUNK - 3, 6)):
            assert r.read_range(start, length) == data[start:start + length]


def test_streaming_writer_matches(tmp_path, key):
    data = secrets.token_bytes(2 * crypto.CHUNK + 999)
    with open(tmp_path / "w.bin", "wb") as f:
        w = crypto.EncryptingWriter(f)
        for i in range(0, len(data), 70_000):
            w.write(data[i:i + 70_000])
        w.close()
    assert crypto.read_all(tmp_path / "w.bin") == data


def test_tampering_and_wrong_keys_are_detected(tmp_path, key):
    data = secrets.token_bytes(3 * crypto.CHUNK)
    sealed = bytearray(crypto.encrypt_bytes(data))
    path = tmp_path / "f.bin"

    flipped = bytearray(sealed)
    flipped[crypto.HEADER_SIZE + crypto.CHUNK + 100] ^= 1
    path.write_bytes(bytes(flipped))
    with pytest.raises(crypto.CryptoError, match="failed authentication"):
        crypto.read_all(path)

    # chunks swapped around, or the last one cut off
    c = crypto.CHUNK + crypto.TAG
    h = crypto.HEADER_SIZE
    swapped = sealed[:h] + sealed[h + c:h + 2 * c] + sealed[h:h + c] + sealed[h + 2 * c:]
    path.write_bytes(bytes(swapped))
    with pytest.raises(crypto.CryptoError):
        crypto.read_all(path)
    path.write_bytes(bytes(sealed[:h + 2 * c]))
    with pytest.raises(crypto.CryptoError):
        crypto.read_all(path)

    path.write_bytes(bytes(sealed))
    crypto.set_key(secrets.token_bytes(32))
    with pytest.raises(crypto.CryptoError, match="different uploads key"):
        crypto.read_all(path)
    crypto.set_key(None)
    with pytest.raises(crypto.CryptoError, match="no uploads key"):
        crypto.read_all(path)


def test_key_file_formats(tmp_path):
    raw = secrets.token_bytes(32)
    assert crypto.parse_key(base64.b64encode(raw) + b"\n") == raw      # openssl rand -base64 32
    assert crypto.parse_key(raw.hex().encode()) == raw
    assert crypto.parse_key(b"") is None and crypto.parse_key(b"\n") is None
    with pytest.raises(ValueError, match="32-byte key"):
        crypto.parse_key(b"too short")
    assert crypto.load_key(tmp_path / "missing") is None
    (tmp_path / "k").write_bytes(base64.b64encode(raw))
    try:
        assert crypto.load_key(tmp_path / "k") == raw and crypto.enabled()
    finally:
        crypto.set_key(None)


async def test_ffmpeg_reads_encrypted_files_through_loopback(tmp_path, key, sample_media):
    plain = sample_media / "h264.mp4"
    enc = tmp_path / "v.mp4"
    enc.write_bytes(crypto.encrypt_bytes(plain.read_bytes()))
    probe = media.parse_video_probe(await media.ffprobe(str(enc)))
    assert probe == media.parse_video_probe(await media.ffprobe(str(plain)))
    shot = tmp_path / "shot.webp"
    await generated.make_screenshot(str(enc), probe["duration"], shot)
    assert Image.open(shot).size == (320, 240)
    assert media.oshash(str(enc)) == media.oshash(str(plain))
    with crypto.ffmpeg_input(str(enc)) as url:
        assert url.startswith("http://127.0.0.1:")
    with crypto.ffmpeg_input(str(plain)) as same:
        assert same == str(plain)


# --- in the app ---------------------------------------------------------------------------

def on_disk_encrypted(path) -> bool:
    return Path(path).read_bytes()[:len(crypto.MAGIC)] == crypto.MAGIC


async def upload_some(app, people, sample_media):
    alice = people["alice"]
    lib = (await alice.post("/api/my/libraries", json={"name": "Private"})).json()["id"]
    video = (sample_media / "h264.mp4").read_bytes()
    pic = (sample_media / "img0.jpg").read_bytes()
    assert (await upload(alice, lib, "clip.mp4", video)).status_code == 201
    assert (await upload(alice, lib, "album/pic.jpg", pic)).status_code == 201
    await drain(app)
    scene = (await alice.get("/api/scenes")).json()["items"][0]
    image = (await alice.get("/api/images")).json()["items"][0]
    return alice, lib, scene, image, video, pic


async def check_everything_works(app, alice, scene, image, video, pic):
    pool = app.state.pool
    # the original, decrypted on the way, with Range support
    r = await alice.get(f"/api/scenes/{scene['id']}/stream")
    assert r.status_code == 200 and r.content == video
    r = await alice.get(f"/api/scenes/{scene['id']}/stream", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == video[100:200]
    # remux through ffmpeg
    r = await alice.get(f"/api/scenes/{scene['id']}/stream.mp4", params={"mode": "remux"})
    assert r.status_code == 200 and b"ftyp" in r.content[:64]
    # previews and thumbnails
    for url in (scene["screenshot_url"], f"/api/scenes/{scene['id']}/screenshot"):
        r = await alice.get(url)
        assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).format == "WEBP"
    assert (await alice.get(f"/api/scenes/{scene['id']}/sprite.vtt")).text.startswith("WEBVTT")
    r = await alice.get(image["thumbnail_url"])
    assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).format == "WEBP"
    r = await alice.get(image["file_url"])
    assert r.status_code == 200 and r.content == pic
    # nothing readable on disk: originals and everything generated from them
    files = [r["path"] for r in await pool.fetch(
        "SELECT f.path FROM files f JOIN library_paths l ON l.id = f.library_id WHERE l.owner_id IS NOT NULL")]
    assert files and all(on_disk_encrypted(p) for p in files)
    root = get_settings().generated_dir
    row = await pool.fetchrow("SELECT f.oshash, f.id FROM scenes s JOIN files f ON f.id = s.file_id WHERE s.id = $1", scene["id"])
    made = [p for p in generated.scene_asset_files(root, generated.scene_version(row["oshash"], row["id"])) if p.exists()]
    irow = await pool.fetchrow("SELECT f.id, f.mod_time FROM images i JOIN files f ON f.id = i.file_id WHERE i.id = $1", image["id"])
    made += [p for p in generated.image_asset_files(root, generated.image_version(irow["id"], irow["mod_time"])) if p.exists()]
    assert len(made) >= 4 and all(on_disk_encrypted(p) for p in made)


async def test_uploads_are_encrypted_from_the_start(app, people, sample_media, key):
    alice, lib, scene, image, video, pic = await upload_some(app, people, sample_media)
    assert scene["duration"] and image["width"] == 64
    await check_everything_works(app, alice, scene, image, video, pic)
    # the main libraries are left alone
    assert not crypto.is_private(get_settings().media_dir / "x.mp4")
    status = (await people["admin"].get("/api/maintenance/encryption")).json()
    assert status["enabled"] and status["uploaded_files"] == status["encrypted_files"] == 2


async def test_turning_encryption_on_later_encrypts_what_is_there(app, people, sample_media):
    pool = app.state.pool
    alice, lib, scene, image, video, pic = await upload_some(app, people, sample_media)
    await alice.get(image["thumbnail_url"])
    paths = [r["path"] for r in await pool.fetch(
        "SELECT f.path FROM files f JOIN library_paths l ON l.id = f.library_id WHERE l.owner_id IS NOT NULL")]
    assert not any(on_disk_encrypted(p) for p in paths)
    mtimes = {p: os.stat(p).st_mtime_ns for p in paths}
    admin = people["admin"]
    assert (await admin.post("/api/maintenance/encryption/run")).status_code == 409    # no key yet

    crypto.set_key(KEY)
    try:
        assert (await admin.get("/api/maintenance/encryption")).json()["encrypted_files"] == 0
        assert (await admin.post("/api/maintenance/encryption/run")).status_code == 202
        await drain(app)
        job = await pool.fetchrow("SELECT status, message FROM jobs WHERE kind = 'encrypt_uploads' ORDER BY id DESC LIMIT 1")
        assert job["status"] == "done" and job["message"].startswith("encrypted 2 uploaded files")
        assert all(on_disk_encrypted(p) and os.stat(p).st_mtime_ns == mtimes[p] for p in paths)
        await check_everything_works(app, alice, scene, image, video, pic)
        # a rescan sees nothing changed (same content size and time)
        before = await pool.fetch("SELECT id, size, oshash FROM files ORDER BY id")
        await pool.execute("INSERT INTO jobs (kind, payload) VALUES ('scan', $1)", {"library_id": lib, "settle": 0})
        await drain(app)
        assert await pool.fetch("SELECT id, size, oshash FROM files ORDER BY id") == before
        # running it again changes nothing
        job_id = await pool.fetchval("INSERT INTO jobs (kind, status) VALUES ('encrypt_uploads', 'running') RETURNING id")
        msg = await worker.handle_encrypt_uploads(pool, get_settings(), JobContext(pool, job_id), {})
        assert msg == "encrypted 0 uploaded files and 0 generated files"
        status = (await admin.get("/api/maintenance/encryption")).json()
        assert status["uploaded_files"] == status["encrypted_files"] == 2
    finally:
        crypto.set_key(None)
