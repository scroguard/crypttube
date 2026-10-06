"""Cleanup of removed media, sweeping orphaned generated files, routine maintenance."""

import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from app import generated, maintenance, worker
from app.config import get_settings
from app.jobs import JobContext
from tests.test_media_api import indexed  # noqa: F401  (fixture)


async def run_job(app, kind, payload):
    pool = app.state.pool
    if kind in ("cleanup", "scan"):
        payload = {"settle": 0} | payload   # the fixture files are seconds old
    job_id = await pool.fetchval(
        "INSERT INTO jobs (kind, payload, status) VALUES ($1, $2, 'running') RETURNING id", kind, payload)
    return await worker.HANDLERS[kind](pool, get_settings(), JobContext(pool, job_id), payload)


def generated_files() -> set[str]:
    root = get_settings().generated_dir
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


async def counts(app) -> tuple[int, int, int, int]:
    pool = app.state.pool
    return tuple([await pool.fetchval(f"SELECT count(*) FROM {t}") for t in ("scenes", "images", "galleries", "files")])


async def warm(app, client, indexed):
    """Generate every kind of asset for everything indexed."""
    await run_job(app, "generate", {"missing": True})
    for sid in indexed["scenes"].values():
        assert (await client.get(f"/api/scenes/{sid}/screenshot", params={"w": 320})).status_code == 200
    for iid in indexed["images"].values():
        assert (await client.get(f"/api/images/{iid}/thumbnail")).status_code == 200


# --- the schedule ---------------------------------------------------------------------

def test_is_due():
    tz = timezone(timedelta(hours=-5))
    at = lambda d, h, m=0: datetime(2026, 10, d, h, m, tzinfo=tz)  # noqa: E731
    assert maintenance.is_due(at(2, 4, 1), 4, None)
    assert maintenance.is_due(at(2, 4, 1), 4, at(1, 4, 0))            # yesterday's run: due again
    assert not maintenance.is_due(at(2, 4, 30), 4, at(2, 4, 1))       # already ran in this slot
    assert not maintenance.is_due(at(2, 3, 59), 4, at(1, 4, 1))       # not yet 04:00 today
    assert maintenance.is_due(at(3, 9), 4, at(1, 4, 1))               # worker was down at 04:00: catch up
    assert not maintenance.is_due(at(2, 9), 4, at(2, 8))              # ran by hand after the slot


async def test_schedule_queues_one_job_per_day(app, clean):
    pool = app.state.pool
    now = datetime(2026, 10, 2, 4, 5).astimezone()
    queued = lambda: pool.fetchval("SELECT count(*) FROM jobs WHERE kind = 'maintenance'")  # noqa: E731
    assert await maintenance.enqueue_if_due(pool, now) is True
    assert await maintenance.enqueue_if_due(pool, now + timedelta(minutes=1)) is False
    assert await maintenance.enqueue_if_due(pool, now + timedelta(hours=20)) is False
    assert await queued() == 1
    await pool.execute("UPDATE jobs SET status = 'done'")
    assert await maintenance.enqueue_if_due(pool, now + timedelta(days=1)) is True
    assert await queued() == 2
    # switched off: nothing is queued
    await maintenance.set_state(pool, maintenance.SETTINGS_KEY, {"enabled": False})
    assert await maintenance.enqueue_if_due(pool, now + timedelta(days=5)) is False
    assert await queued() == 2


# --- cleanup --------------------------------------------------------------------------

async def test_cleanup_dry_run_then_real(app, client, indexed):
    pool = app.state.pool
    await warm(app, client, indexed)
    sid = indexed["scenes"]["h264"]
    await client.patch(f"/api/scenes/{sid}", json={"rating": 80})
    before_counts, before_files = await counts(app), generated_files()
    assert before_counts == (3, 6, 2, 10)

    # nothing is gone: a cleanup finds nothing to do and removes nothing
    msg = await run_job(app, "cleanup", {"dry_run": False})
    assert msg.startswith("removed 0 scenes, 0 images, 0 galleries and 0 generated files")
    assert (await counts(app), generated_files()) == (before_counts, before_files)

    # outside the app: one video, the pics folder and the zip are deleted
    root = indexed["root"]
    (root / "videos" / "h264.mp4").unlink()
    shutil.rmtree(root / "pics")
    (root / "archive.zip").unlink()

    # dry run: reports, changes nothing (the rescan marks files missing, which is not a removal)
    msg = await run_job(app, "cleanup", {"dry_run": True})
    assert msg.startswith("would remove 1 scenes, 6 images, 2 galleries and ")
    report = (await client.get("/api/maintenance")).json()["cleanup_report"]
    assert report["dry_run"] is True
    assert report["removed"] == {"scenes": 1, "images": 6, "galleries": 2, "files": 6,
                                 "still_on_disk": 0, "libraries_skipped": 0}
    # the scene's screenshot, sprite, vtt, card thumbnail and the 320 one asked for by warm();
    # a grid thumbnail per image, plus a cover thumbnail for each gallery's cover
    assert report["generated"]["scene_assets"]["files"] == 5
    assert report["generated"]["image_thumbnails"]["files"] == 8
    assert report["generated"]["files"] == 13 and report["generated"]["bytes"] > 0
    assert (await counts(app), generated_files()) == (before_counts, before_files)
    assert (await client.get("/api/maintenance")).json()["missing_files"] == 6

    # for real
    msg = await run_job(app, "cleanup", {"dry_run": False})
    assert msg.startswith("removed 1 scenes, 6 images, 2 galleries and 13 generated files")
    assert await counts(app) == (2, 0, 0, 2)
    after = generated_files()
    assert len(before_files - after) == 13 and after < before_files
    # what is left belongs to the two remaining scenes, which still work
    for codec in ("hevc", "vp9"):
        assert (await client.get(f"/api/scenes/{indexed['scenes'][codec]}/screenshot")).status_code == 200
    assert generated_files() == after
    assert (await client.get(f"/api/scenes/{sid}")).status_code == 404
    # no empty shard folders are left behind
    gen = get_settings().generated_dir
    assert all(any(d.iterdir()) for d in (gen / "thumbs" / "scenes").iterdir())
    assert not (gen / "thumbs" / "images").exists() or not any((gen / "thumbs" / "images").iterdir())

    # and again: nothing more to do
    assert (await run_job(app, "cleanup", {"dry_run": False})).startswith("removed 0 scenes, 0 images, 0 galleries and 0 ")


async def test_moved_files_keep_their_metadata(app, client, indexed):
    sid = indexed["scenes"]["h264"]
    await client.patch(f"/api/scenes/{sid}", json={"rating": 60})
    root = indexed["root"]
    (root / "renamed").mkdir()
    (root / "videos" / "h264.mp4").rename(root / "renamed" / "moved.mp4")
    await run_job(app, "cleanup", {"dry_run": False})
    scene = (await client.get(f"/api/scenes/{sid}")).json()
    assert scene["rating"] == 60 and scene["file"]["path"].endswith("renamed/moved.mp4")


async def test_unavailable_or_disabled_library_is_left_alone(app, client, indexed):
    pool = app.state.pool
    root = indexed["root"]
    before = await counts(app)
    mark = "UPDATE files SET missing_since = now() WHERE zip_file_id IS NULL"
    await pool.execute(mark)   # as if a scan had seen an empty mount

    # the files are in fact on disk: kept
    report = await maintenance.cleanup(pool, get_settings().generated_dir, None, dry_run=False)
    assert report["removed"]["files"] == 0 and report["removed"]["still_on_disk"] == 8
    assert (await counts(app))[0] == before[0]

    # the library folder is gone entirely (unmounted drive): nothing is touched
    hidden = root.with_name(root.name + "-unmounted")
    root.rename(hidden)
    try:
        await pool.execute(mark)
        msg = await run_job(app, "cleanup", {"dry_run": False})
        assert "1 disabled or unavailable libraries were left alone" in msg
        assert await counts(app) == before
    finally:
        hidden.rename(root)

    # a disabled library is not touched either, even if its files are really gone
    (root / "videos" / "h264.mp4").unlink()
    await pool.execute("UPDATE library_paths SET enabled = false")
    report = await maintenance.cleanup(pool, get_settings().generated_dir, None, dry_run=False)
    assert report["removed"]["libraries_skipped"] == 1 and await counts(app) == before


async def test_missing_for_n_days(app, client, indexed):
    pool = app.state.pool
    (indexed["root"] / "videos" / "h264.mp4").unlink()
    (indexed["root"] / "videos" / "vp9.webm").unlink()
    await run_job(app, "scan", {})
    await pool.execute("UPDATE files SET missing_since = now() - interval '10 days' WHERE path LIKE '%h264.mp4'")
    msg = await run_job(app, "cleanup", {"dry_run": False, "min_missing_days": 7})
    assert msg.startswith("removed 1 scenes")
    assert {r["video_codec"] for r in await pool.fetch("SELECT video_codec FROM scenes")} == {"hevc", "vp9"}


async def test_sweep_entity_images_temp_files_and_broken_links(app, client, clean):
    pool = app.state.pool
    gen = get_settings().generated_dir
    keep = (await client.post("/api/performers", json={"name": "Keeps Picture"})).json()["id"]
    lost = (await client.post("/api/studios", json={"name": "Lost Picture"})).json()["id"]

    def webp(rel: str) -> Path:
        path = gen / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8)).save(path, "WEBP")
        return path

    kept = webp(f"entities/performers/{keep}.webp")
    await pool.execute("UPDATE performers SET image_path = $2 WHERE id = $1", keep, f"entities/performers/{keep}.webp")
    await pool.execute("UPDATE studios SET image_path = $2 WHERE id = $1", lost, f"entities/studios/{lost}.webp")
    orphan = webp("entities/performers/99999.webp")              # performer deleted behind the app's back
    stranger = webp("thumbs/scenes/ab/0123456789abcdef-w320.webp")  # asset of a scene that doesn't exist
    old_tmp, new_tmp = webp("screenshots/ab/.tmp-old"), webp("screenshots/ab/.tmp-new")
    upload = webp("tmp/stash-abandoned.sqlite")
    foreign = gen / "notes.txt"                                    # not ours: never touched
    foreign.write_text("hello")
    long_ago = time.time() - 86400
    for p in (old_tmp, upload):
        os.utime(p, (long_ago, long_ago))

    report = await maintenance.sweep_generated(pool, gen, delete=False)
    assert report["entity_images"]["files"] == 1 and report["scene_assets"]["files"] == 1
    assert report["temporary"]["files"] == 2 and report["links_fixed"] == 1 and report["files"] == 4
    assert all(p.exists() for p in (kept, orphan, stranger, old_tmp, new_tmp, upload))

    report = await maintenance.sweep_generated(pool, gen, delete=True)
    assert report["files"] == 4
    assert kept.exists() and new_tmp.exists() and foreign.exists()
    assert not any(p.exists() for p in (orphan, stranger, old_tmp, upload))
    assert await pool.fetchval("SELECT image_path FROM studios WHERE id = $1", lost) is None
    assert await pool.fetchval("SELECT image_path FROM performers WHERE id = $1", keep) is not None
    assert (await maintenance.sweep_generated(pool, gen, delete=True))["files"] == 0


# --- routine maintenance --------------------------------------------------------------

async def test_maintenance_job(app, client, indexed):
    pool = app.state.pool
    uid = client.user_id
    await pool.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES ('\\x01', $1, now() - interval '1 day')", uid)
    await pool.execute(
        """INSERT INTO jobs (kind, status, finished_at) VALUES
           ('scan', 'done', now() - interval '8 days'), ('scan', 'done', now() - interval '1 day'),
           ('scan', 'failed', now() - interval '8 days'), ('scan', 'failed', now() - interval '31 days')""")
    orphan = get_settings().generated_dir / "screenshots" / "zz" / "ffffffffffffffff.webp"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"x")
    (indexed["root"] / "videos" / "h264.mp4").unlink()
    await run_job(app, "scan", {})

    msg = await run_job(app, "maintenance", {})
    assert "removed 1 expired sessions and 2 old jobs" in msg and "swept 1 generated files" in msg
    assert not orphan.exists()
    assert (await client.get("/api/auth/status")).json()["user"]["username"]      # live session untouched
    # missing files are only forgotten automatically when that is switched on
    assert (await counts(app))[0] == 3
    report = (await client.get("/api/maintenance")).json()["maintenance_report"]
    assert report["tables_optimized"] > 20 and report["database_bytes"] > 0 and "removed" not in report
    analyzed = await pool.fetchval("SELECT last_analyze FROM pg_stat_user_tables WHERE relname = 'scenes'")
    assert analyzed is not None

    r = await client.put("/api/maintenance/settings", json={"enabled": True, "hour": 3, "remove_missing_after_days": 5})
    assert r.status_code == 200
    await run_job(app, "maintenance", {})
    assert (await counts(app))[0] == 3                           # missing for less than 5 days
    await pool.execute("UPDATE files SET missing_since = now() - interval '6 days' WHERE missing_since IS NOT NULL")
    msg = await run_job(app, "maintenance", {})
    assert "forgot 1 scenes, 0 images, 0 galleries" in msg and (await counts(app))[0] == 2


async def test_api(app, client, clean, login_as, anon):
    body = (await client.get("/api/maintenance")).json()
    assert body["settings"] == {"enabled": True, "hour": 4, "remove_missing_after_days": 0}
    assert body["cleanup_report"] is None and body["jobs"] == {} and body["database_bytes"] > 0

    assert (await client.put("/api/maintenance/settings",
                             json={"enabled": True, "hour": 24, "remove_missing_after_days": 0})).status_code == 422
    r = await client.post("/api/maintenance/cleanup")
    assert r.status_code == 202
    again = await client.post("/api/maintenance/cleanup", json={"dry_run": True})
    assert again.json()["job_id"] == r.json()["job_id"]            # one at a time
    job = await app.state.pool.fetchrow("SELECT kind, payload FROM jobs WHERE id = $1", r.json()["job_id"])
    assert job["kind"] == "cleanup" and job["payload"]["dry_run"] is True   # safe by default
    assert (await client.post("/api/maintenance/run")).status_code == 202
    assert set((await client.get("/api/maintenance")).json()["jobs"]) == {"cleanup", "maintenance"}

    for c in (await login_as("ed", "editor"), await login_as("vera", "viewer")):
        assert (await c.get("/api/maintenance")).status_code == 403
        assert (await c.post("/api/maintenance/cleanup", json={"dry_run": False})).status_code == 403
        assert (await c.post("/api/maintenance/run")).status_code == 403
    assert (await anon.get("/api/maintenance")).status_code == 401


async def test_cleanup_refuses_while_files_are_still_copying(app, client, indexed):
    """A moved file that is still being written can't be matched to its old entry yet."""
    (indexed["root"] / "videos" / "h264.mp4").rename(indexed["root"] / "videos" / "moving.mp4")
    pool = app.state.pool
    job_id = await pool.fetchval("INSERT INTO jobs (kind, status) VALUES ('cleanup', 'running') RETURNING id")
    try:
        await worker.handle_cleanup(pool, get_settings(), JobContext(pool, job_id), {"dry_run": False})
        raise AssertionError("expected the cleanup to refuse")
    except worker.ScanError as e:
        assert "may still be copying; nothing was removed" in str(e)
    assert (await counts(app))[0] == 3
