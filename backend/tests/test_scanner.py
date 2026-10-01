import os
import shutil
from pathlib import Path

import pytest

from app.config import get_settings
from app.scanner import Scanner, ScanError


@pytest.fixture
async def library(app, clean, library_dir):
    lid = await app.state.pool.fetchval(
        "INSERT INTO library_paths (path) VALUES ($1) RETURNING id", str(library_dir)
    )
    return lid, library_dir


async def scan(app, library_id, subpath=None, **kw):
    kw.setdefault("settle_seconds", 0)
    return await Scanner(app.state.pool, get_settings().generated_dir, **kw).scan(library_id, subpath)


async def test_initial_scan_indexes_everything(app, library):
    lid, root = library
    stats = await scan(app, lid)
    assert (stats.new, stats.failed) == (7, 0)  # 3 videos + 3 loose images + 1 zip

    pool = app.state.pool
    codecs = {r["video_codec"]: r for r in await pool.fetch("SELECT * FROM scenes")}
    assert set(codecs) == {"h264", "hevc", "vp9"}
    h264 = codecs["h264"]
    assert (h264["width"], h264["height"]) == (320, 240)
    assert h264["framerate"] == pytest.approx(25)
    assert h264["duration"] == pytest.approx(1, abs=0.1)
    assert h264["audio_codec"] == "aac"

    folder = await pool.fetchrow("SELECT * FROM galleries WHERE folder_path = $1", str(root / "pics"))
    assert folder["title"] == "pics" and folder["image_count"] == 3
    order = await pool.fetch(
        """SELECT f.path FROM gallery_images gi JOIN images i ON i.id = gi.image_id
           JOIN files f ON f.id = i.file_id WHERE gi.gallery_id = $1 ORDER BY gi.position""",
        folder["id"],
    )
    assert [os.path.basename(r["path"]) for r in order] == ["img1.jpg", "img2.jpg", "img10.jpg"]
    assert folder["cover_image_id"] is not None

    z = await pool.fetchrow("SELECT * FROM galleries WHERE zip_file_id IS NOT NULL")
    assert z["title"] == "archive" and z["image_count"] == 2  # __MACOSX + .txt skipped
    members = await pool.fetch(
        """SELECT f.path FROM gallery_images gi JOIN images i ON i.id = gi.image_id
           JOIN files f ON f.id = i.file_id WHERE gi.gallery_id = $1 ORDER BY gi.position""",
        z["id"],
    )
    assert [r["path"] for r in members] == ["a.jpg", "b.jpg"]


async def test_rescan_without_changes_is_a_noop(app, library):
    lid, _ = library
    await scan(app, lid)
    stats = await scan(app, lid)
    assert stats.summary() == "no changes"


async def test_moved_file_keeps_its_scene(app, library):
    lid, root = library
    await scan(app, lid)
    pool = app.state.pool
    await pool.execute("UPDATE scenes SET title = 'kept' WHERE video_codec = 'h264'")
    (root / "elsewhere").mkdir()
    shutil.move(root / "videos" / "h264.mp4", root / "elsewhere" / "renamed.mp4")

    stats = await scan(app, lid)
    assert (stats.moved, stats.new, stats.missing) == (1, 0, 0)
    row = await pool.fetchrow(
        "SELECT s.title, f.path FROM scenes s JOIN files f ON f.id = s.file_id WHERE s.video_codec = 'h264'"
    )
    assert row["title"] == "kept"
    assert row["path"] == str(root / "elsewhere" / "renamed.mp4")


async def test_moved_image_changes_folder_gallery(app, library):
    lid, root = library
    await scan(app, lid)
    (root / "pics2").mkdir()
    shutil.move(root / "pics" / "img1.jpg", root / "pics2" / "img1.jpg")
    stats = await scan(app, lid)
    assert stats.moved == 1
    counts = dict(await app.state.pool.fetch("SELECT title, image_count FROM galleries WHERE folder_path IS NOT NULL"))
    assert counts == {"pics": 2, "pics2": 1}


async def test_deleted_file_is_soft_missing_then_reappears(app, library):
    lid, root = library
    await scan(app, lid)
    pool = app.state.pool
    stash = root.parent / (root.name + "-stash.mkv")
    shutil.move(root / "videos" / "h265.mkv", stash)
    try:
        assert (await scan(app, lid)).missing == 1
        assert await pool.fetchval("SELECT count(*) FROM scenes") == 3  # metadata retained
        assert await pool.fetchval("SELECT count(*) FROM files WHERE missing_since IS NOT NULL") == 1
        shutil.move(stash, root / "videos" / "h265.mkv")
        stats = await scan(app, lid)
        assert stats.reappeared == 1 or stats.moved == 1
        assert await pool.fetchval("SELECT count(*) FROM files WHERE missing_since IS NOT NULL") == 0
    finally:
        stash.unlink(missing_ok=True)


async def test_unavailable_library_root_marks_nothing_missing(app, library):
    lid, root = library
    await scan(app, lid)
    hidden = root.with_name(root.name + "-unmounted")
    root.rename(hidden)
    try:
        with pytest.raises(ScanError):
            await scan(app, lid)
        assert await app.state.pool.fetchval("SELECT count(*) FROM files WHERE missing_since IS NOT NULL") == 0
    finally:
        hidden.rename(root)


async def test_changed_file_is_reprobed(app, library, sample_media):
    lid, root = library
    await scan(app, lid)
    shutil.copy(sample_media / "vp9.webm", root / "videos" / "h264.mp4")  # content replaced in place
    stats = await scan(app, lid)
    assert stats.updated == 1
    codecs = sorted(r["video_codec"] for r in await app.state.pool.fetch("SELECT video_codec FROM scenes"))
    assert codecs == ["hevc", "vp9", "vp9"]


async def test_subpath_scan_only_touches_subtree(app, library):
    lid, root = library
    await scan(app, lid)
    (root / "pics" / "img2.jpg").unlink()
    (root / "videos" / "vp9.webm").unlink()
    stats = await scan(app, lid, str(root / "videos"))
    assert stats.missing == 1  # the image outside the subtree isn't examined


async def test_unsettled_files_are_deferred(app, library):
    lid, _ = library
    stats = await scan(app, lid, settle_seconds=3600)
    assert stats.new == 0 and stats.unsettled == 7
    assert await app.state.pool.fetchval("SELECT count(*) FROM files") == 0


async def test_corrupt_files_fail_without_aborting(app, library):
    lid, root = library
    (root / "videos" / "broken.mp4").write_bytes(b"not a video" * 100)
    (root / "broken.zip").write_bytes(b"not a zip")
    stats = await scan(app, lid)
    assert stats.failed == 2
    assert stats.new == 7
    assert len(stats.errors) == 2


async def test_exclude_patterns_and_hidden_files(app, library):
    lid, root = library
    await app.state.pool.execute(
        "UPDATE library_paths SET exclude_patterns = '{videos/*.mkv, pics}' WHERE id = $1", lid
    )
    (root / ".hidden").mkdir()
    shutil.copy(root / "videos" / "h264.mp4", root / ".hidden" / "x.mp4")
    stats = await scan(app, lid)
    assert stats.new == 3  # h264.mp4, vp9.webm, archive.zip


async def test_generated_dir_is_never_scanned(app, clean, library_dir, sample_media, monkeypatch):
    generated = library_dir / "generated"
    generated.mkdir()
    shutil.copy(sample_media / "h264.mp4", generated / "preview.mp4")
    lid = await app.state.pool.fetchval(
        "INSERT INTO library_paths (path) VALUES ($1) RETURNING id", str(library_dir))
    stats = await Scanner(app.state.pool, generated, settle_seconds=0).scan(lid)
    assert stats.new == 7
    paths = [r["path"] for r in await app.state.pool.fetch("SELECT path FROM files")]
    assert not any("/generated/" in p for p in paths)


async def test_many_images_with_a_small_pool_do_not_deadlock(app, clean, library_dir, sample_media):
    """Regression: each image used to hold a pooled connection while waiting for a
    second one (folder gallery lookup); with more concurrent files than connections,
    the scan hung forever."""
    import asyncio
    from app.db import create_pool
    for i in range(40):
        shutil.copy(sample_media / "img0.jpg", library_dir / "pics" / f"extra{i}.jpg")
    lid = await app.state.pool.fetchval(
        "INSERT INTO library_paths (path) VALUES ($1) RETURNING id", str(library_dir))
    small = await create_pool(get_settings().db_params(), min_size=1, max_size=2)
    try:
        stats = await asyncio.wait_for(
            Scanner(small, get_settings().generated_dir, settle_seconds=0).scan(lid), timeout=60)
    finally:
        await small.close()
    assert stats.new == 47 and stats.failed == 0


async def test_scan_reads_files_with_bounded_concurrency(app, clean, library_dir, sample_media, monkeypatch):
    """A first scan of a big folder must not read every file at once."""
    import shutil
    import threading
    import time
    from app import media
    for i in range(120):
        shutil.copy(sample_media / "img0.jpg", library_dir / "pics" / f"bulk{i}.jpg")
    lock, state = threading.Lock(), {"now": 0, "peak": 0}

    def tracked(fn):
        def wrapper(*args, **kwargs):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            try:
                time.sleep(0.002)
                return fn(*args, **kwargs)
            finally:
                with lock:
                    state["now"] -= 1
        return wrapper

    monkeypatch.setattr(media, "oshash", tracked(media.oshash))
    monkeypatch.setattr(media, "probe_image", tracked(media.probe_image))
    pool = app.state.pool
    lid = await pool.fetchval("INSERT INTO library_paths (path) VALUES ($1) RETURNING id", str(library_dir))
    stats = await Scanner(pool, get_settings().generated_dir, settle_seconds=0, probe_concurrency=3).scan(lid)
    assert stats.new >= 120 and stats.failed == 0
    assert state["peak"] <= 3


def test_helper_processes_run_at_low_priority():
    from app import media
    cmd = media.background(["ffprobe", "x"])
    assert cmd[-2:] == ["ffprobe", "x"] and cmd[:3] == ["nice", "-n", "15"] and "ionice" in cmd
