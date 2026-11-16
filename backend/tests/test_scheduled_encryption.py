"""Encryption of users' uploads on a schedule (app.upload_encryption and the encrypt_uploads job)."""

import io
import os
import time
from pathlib import Path

import pytest
from PIL import Image

from app import crypto, generated, upload_encryption, worker
from app.config import get_settings
from app.jobs import JobContext
from tests.test_crypto import KEY, check_everything_works, on_disk_encrypted, upload_some
from tests.test_user_libraries import drain, people, upload  # noqa: F401  (fixtures)

HOUR_AGO = time.time() - 3600


@pytest.fixture
def key():
    crypto.set_key(KEY)
    yield KEY
    crypto.set_key(None)


async def run_job(pool, payload=None, **settings) -> str:
    job_id = await pool.fetchval("INSERT INTO jobs (kind, status) VALUES ('encrypt_uploads', 'running') RETURNING id")
    s = get_settings().model_copy(update=settings)
    return await worker.handle_encrypt_uploads(pool, s, JobContext(pool, job_id), payload or {"scheduled": True})


def copy_in(root: Path, rel: str, data: bytes, mtime: float = HOUR_AGO) -> Path:
    """A file that reached the library without the app (plaintext, not indexed)."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))
    return p


async def new_lib(app, alice) -> tuple[int, Path]:
    lib = (await alice.post("/api/my/libraries", json={"name": "Copied"})).json()["id"]
    return lib, Path(await app.state.pool.fetchval("SELECT path FROM library_paths WHERE id = $1", lib))


async def test_schedule_queues_only_with_a_key_and_one_at_a_time(app, clean):
    pool = app.state.pool
    assert await upload_encryption.enqueue_if_due(pool, 15) is None             # no key: never
    crypto.set_key(KEY)
    try:
        assert await upload_encryption.enqueue_if_due(pool, 0) is None          # interval 0: off
        job = await upload_encryption.enqueue_if_due(pool, 15)
        assert job is not None
        assert await upload_encryption.enqueue_if_due(pool, 15) is None         # one queued already
        await pool.execute("UPDATE jobs SET status = 'running' WHERE id = $1", job)
        assert await upload_encryption.enqueue_if_due(pool, 15) is None         # running: no overlap
        await pool.execute("UPDATE jobs SET status = 'done', finished_at = now() WHERE id = $1", job)
        assert await upload_encryption.enqueue_if_due(pool, 15) is None         # ran within 15 min
        await pool.execute("UPDATE jobs SET created_at = now() - interval '16 minutes' WHERE id = $1", job)
        assert await upload_encryption.enqueue_if_due(pool, 15) is not None     # due again
    finally:
        crypto.set_key(None)


async def test_off_without_a_key(app, people, sample_media):
    alice = people["alice"]
    _, root = await new_lib(app, alice)
    p = copy_in(root, "clip.mp4", (sample_media / "h264.mp4").read_bytes())
    assert (await run_job(app.state.pool)).startswith("encryption of user uploads is off")
    assert not on_disk_encrypted(p)


async def test_a_file_encrypted_before_it_was_scanned_is_then_indexed(app, people, sample_media, key):
    pool, alice = app.state.pool, people["alice"]
    lib, root = await new_lib(app, alice)
    video = (sample_media / "h264.mp4").read_bytes()
    pic = (sample_media / "img0.jpg").read_bytes()
    clip, photo = copy_in(root, "clip.mp4", video), copy_in(root, "album/pic.jpg", pic)
    fresh = copy_in(root, "album/new.jpg", pic, mtime=time.time())   # still being copied, maybe
    mtime = clip.stat().st_mtime_ns

    msg = await run_job(pool)
    assert msg.startswith("encrypted 2 uploaded files") and "1 changed in the last 60 s" in msg
    assert on_disk_encrypted(clip) and on_disk_encrypted(photo) and not on_disk_encrypted(fresh)
    assert clip.stat().st_mtime_ns == mtime and not list(root.rglob(".enc-*"))
    assert await pool.fetchval("SELECT count(*) FROM files WHERE library_id = $1", lib) == 0   # not indexed yet
    # the fresh one is taken by the next run, once it has settled
    os.utime(fresh, (HOUR_AGO, HOUR_AGO))
    assert (await run_job(pool)).startswith("encrypted 1 uploaded files")
    assert on_disk_encrypted(fresh)

    # the owner scans: the encrypted files are indexed by their content
    assert (await alice.post(f"/api/my/libraries/{lib}/scan")).status_code == 202
    await drain(app)
    sizes = {r["path"]: r["size"] for r in await pool.fetch("SELECT path, size FROM files WHERE library_id = $1", lib)}
    assert sizes[str(clip)] == len(video) and sizes[str(photo)] == len(pic)
    scene = (await alice.get("/api/scenes")).json()["items"][0]
    image = next(i for i in (await alice.get("/api/images")).json()["items"] if i["title"].startswith("pic"))
    assert scene["duration"] and image["width"] == 64
    await check_everything_works(app, alice, scene, image, video, pic)   # plays, previews, nothing plain on disk


async def test_a_scanned_plaintext_file_keeps_its_index_and_previews(app, people, sample_media):
    pool = app.state.pool
    alice, lib, scene, image, video, pic = await upload_some(app, people, sample_media)   # no key yet: plaintext
    await alice.get(image["thumbnail_url"])
    before = await pool.fetch("SELECT id, path, size, mod_time, oshash FROM files ORDER BY id")
    scenes = await pool.fetch("SELECT id, file_id FROM scenes ORDER BY id")
    images = await pool.fetch("SELECT id, file_id FROM images ORDER BY id")
    root = get_settings().generated_dir
    row = await pool.fetchrow("SELECT f.oshash, f.id FROM scenes s JOIN files f ON f.id = s.file_id WHERE s.id = $1", scene["id"])
    assets = [p for p in generated.scene_asset_files(root, generated.scene_version(row["oshash"], row["id"])) if p.exists()]
    assert assets and not any(on_disk_encrypted(p) for p in assets)

    crypto.set_key(KEY)
    try:
        # a scheduled run (copied-in files and these old plaintext ones alike), then a rescan
        assert (await run_job(pool, encrypt_settle_seconds=0)).startswith("encrypted 2 uploaded files")
        await pool.execute("INSERT INTO jobs (kind, payload) VALUES ('scan', $1)", {"library_id": lib, "settle": 0})
        await drain(app)
        assert await pool.fetch("SELECT id, path, size, mod_time, oshash FROM files ORDER BY id") == before
        assert await pool.fetch("SELECT id, file_id FROM scenes ORDER BY id") == scenes
        assert await pool.fetch("SELECT id, file_id FROM images ORDER BY id") == images
        assert all(p.exists() and on_disk_encrypted(p) for p in assets)      # the same previews, now encrypted
        await check_everything_works(app, alice, scene, image, video, pic)
        assert (await run_job(pool, encrypt_settle_seconds=0)) == "encrypted 0 uploaded files and 0 generated files"   # never twice
    finally:
        crypto.set_key(None)


async def test_a_file_that_changes_while_being_encrypted_is_left_alone(tmp_path, key, monkeypatch):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"old" * 1000)
    real_close = crypto.EncryptingWriter.close

    def close_and_replace(self):
        real_close(self)
        replacement = tmp_path / "upload"
        replacement.write_bytes(b"new upload")
        os.replace(replacement, p)                      # an upload lands mid-encryption

    monkeypatch.setattr(crypto.EncryptingWriter, "close", close_and_replace)
    assert crypto.encrypt_file_in_place(p) is False
    assert p.read_bytes() == b"new upload" and [f.name for f in tmp_path.iterdir()] == ["clip.mp4"]


async def test_maintenance_shows_last_run_and_whats_left(app, people, sample_media, key):
    pool, alice, admin = app.state.pool, people["alice"], people["admin"]
    _, root = await new_lib(app, alice)
    copy_in(root, "a.mp4", (sample_media / "h264.mp4").read_bytes())
    copy_in(root, "b.jpg", (sample_media / "img0.jpg").read_bytes())
    status = (await admin.get("/api/maintenance/encryption")).json()
    assert status["plaintext_files"] is None and status["interval_minutes"] == 15 and status["last_run"] is None
    await pool.execute("INSERT INTO jobs (kind, payload, dedupe_key) VALUES ('encrypt_uploads', '{\"scheduled\": true}', 'encrypt_uploads')")
    await drain(app)    # the worker's own settings: these files are an hour old, so they're taken
    status = (await admin.get("/api/maintenance/encryption")).json()
    assert status["plaintext_files"] == 0
    assert status["last_run"]["status"] == "done" and status["last_run"]["message"].startswith("encrypted 2 uploaded files")
