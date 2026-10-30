"""Background worker: runs queued jobs one at a time and hosts the filesystem watcher.

Run as its own container (`python -m app.worker`) so scans and ffmpeg work never
compete with the API process for the event loop.
"""

import asyncio
import io
import logging
import os
import signal
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor

import asyncpg

from app import counts, crypto, folder_links, generated, jobs, maintenance
from app.config import Settings, get_settings
from app.db import create_pool, migrate
from app.deletion import DeleteError
from app.media import ProbeError
from app.scanner import ScanError, Scanner, ScanStats
from app.watcher import LibraryWatcher, scan_dedupe_key

log = logging.getLogger("worker")

LIBRARIES_CHANNEL = "libraries_changed"


async def handle_scan(pool: asyncpg.Pool, settings: Settings, ctx: jobs.JobContext, payload: dict,
                      totals: ScanStats | None = None) -> str:
    library_id = payload.get("library_id")
    path = payload.get("path")
    if library_id is None:
        library_ids = [r["id"] for r in await pool.fetch(
            "SELECT id FROM library_paths WHERE enabled ORDER BY id")]
    else:
        library_ids = [library_id]

    results, unavailable = [], []
    for lid in library_ids:
        if await ctx.cancelled():
            break
        # Uploads are complete when the scan is requested, so they skip the settle wait.
        scanner = Scanner(pool, settings.generated_dir, ctx=ctx,
                          settle_seconds=payload.get("settle", settings.scan_settle_seconds),
                          probe_concurrency=settings.scan_probe_concurrency)
        try:
            stats: ScanStats = await scanner.scan(lid, path)
        except ScanError as e:
            if len(library_ids) == 1 and totals is None:   # (a cleanup carries on without it)
                raise
            unavailable.append(str(e))
            continue
        results.append(stats)
        # Files still being written are retried once they have had time to settle.
        # This only re-enqueues while unsettled files exist, so it terminates.
        if stats.unsettled:
            await jobs.enqueue(pool, "scan", {"library_id": lid, "path": path},
                               dedupe_key=scan_dedupe_key(lid, path),
                               delay=settings.scan_settle_seconds + 1)
        for err in stats.errors:
            log.warning("scan error: %s", err)

    # Scan -> generate / refresh_counts only; neither of those enqueues anything.
    scene_ids = [i for s in results for i in s.scene_ids]
    image_ids = [i for s in results for i in s.image_ids]
    if scene_ids or image_ids:
        await jobs.enqueue(pool, "generate", {"scene_ids": scene_ids, "image_ids": image_ids})
    if any(s.summary() != "no changes" for s in results):
        await counts.request_refresh(pool)

    total = totals if totals is not None else ScanStats()
    for s in results:
        for k, v in vars(s).items():
            if isinstance(v, int):
                setattr(total, k, getattr(total, k) + v)
    message = total.summary()
    if total.unreadable:
        message += f"; {total.unreadable} known unreadable files skipped (see Admin → Maintenance)"
    if unavailable:
        message += "; skipped: " + "; ".join(unavailable)
    return message


GENERATE_BATCH = 200
MAX_REPORTED_ERRORS = 10


async def _generate_candidates(pool: asyncpg.Pool, payload: dict):
    """Yield batches of scenes in scope: explicit ids, one library, or all (paged by id)."""
    conds, args = ["f.missing_since IS NULL"], []
    if "scene_ids" in payload:
        args.append(payload["scene_ids"])
        conds.append(f"s.id = ANY(${len(args)}::bigint[])")
    if payload.get("library_id") is not None:
        args.append(payload["library_id"])
        conds.append(f"s.library_id = ${len(args)}")
    query = f"""SELECT s.id, s.duration, s.assets_rev, s.is_live_photo, f.id AS file_id, f.path, f.oshash,
                       l.owner_id AS library_owner
                FROM scenes s JOIN files f ON f.id = s.file_id JOIN library_paths l ON l.id = s.library_id
                WHERE {' AND '.join(conds)} AND s.id > ${len(args) + 1}
                ORDER BY s.id LIMIT {GENERATE_BATCH}"""
    after = 0
    while rows := await pool.fetch(query, *args, after):
        yield rows
        after = rows[-1]["id"]


async def _reset_image_thumbnails(pool: asyncpg.Pool, root, library_id: int | None) -> None:
    """Forced rebuild for images: drop cached thumbnails (they are recreated on demand)
    and bump the revision so browsers stop using their cached copies."""
    if library_id is None:
        await pool.execute("UPDATE images SET assets_rev = assets_rev + 1")
        return  # the files went with the wipe of thumbs/
    after = 0
    while rows := await pool.fetch(
            """SELECT i.id, f.id AS file_id, f.mod_time FROM images i JOIN files f ON f.id = i.file_id
               WHERE i.library_id = $1 AND i.id > $2 ORDER BY i.id LIMIT 1000""", library_id, after):
        files = [p for r in rows
                 for p in generated.image_asset_files(root, generated.image_version(r["file_id"], r["mod_time"]))]
        await asyncio.to_thread(generated.remove_files, files)
        after = rows[-1]["id"]
    await pool.execute("UPDATE images SET assets_rev = assets_rev + 1 WHERE library_id = $1", library_id)


def _image_thumbnails(root, row) -> int:
    """Make the grid (and, for gallery covers, cover) thumbnails of one image that are
    missing. Reads the original once. Returns how many were made."""
    widths = [generated.IMAGE_GRID_WIDTH] + ([generated.COVER_WIDTH] if row["is_cover"] else [])
    version = generated.image_version(row["file_id"], row["mod_time"])
    todo = [(w, generated.thumbnail_path(root, "images", version, w)) for w in widths]
    todo = [(w, p) for w, p in todo if not generated.usable(p)]
    if not todo:
        return 0
    with generated.open_image_source(row["path"], row["zip_path"]) as src:
        data = src.read()
    for w, path in todo:
        generated.make_thumbnail(io.BytesIO(data), w, path)
    return len(todo)


async def _generate_image_thumbnails(pool: asyncpg.Pool, settings: Settings, ctx: jobs.JobContext,
                                     payload: dict) -> tuple[int, int]:
    """Thumbnails for the images in scope, ahead of anyone looking at them: the images a
    scan found (`image_ids`), one library, or everything. A job for given scenes only
    (`scene_ids` without `image_ids`) does no images."""
    if "image_ids" in payload:
        conds, args = ["i.id = ANY($1::bigint[])"], [payload["image_ids"]]
    elif "scene_ids" in payload:
        return 0, 0
    elif payload.get("library_id") is not None:
        conds, args = ["i.library_id = $1"], [payload["library_id"]]
    else:
        conds, args = [], []
    where = " AND ".join(["f.missing_since IS NULL", "(z.id IS NULL OR z.missing_since IS NULL)", *conds])
    total = await pool.fetchval(
        f"SELECT count(*) FROM images i JOIN files f ON f.id = i.file_id LEFT JOIN files z ON z.id = f.zip_file_id WHERE {where}",
        *args)
    if not total:
        return 0, 0
    root = settings.generated_dir
    sem = asyncio.Semaphore(settings.scan_probe_concurrency)
    made = failed = done = 0

    async def one(row):
        nonlocal made, failed
        async with sem:
            try:
                with crypto.private_outputs(row["zip_path"] or row["path"]):
                    made += await asyncio.to_thread(_image_thumbnails, root, row)
            except (ProbeError, OSError, KeyError, zipfile.BadZipFile):
                failed += 1

    after = 0
    while rows := await pool.fetch(
            f"""SELECT i.id, f.id AS file_id, f.mod_time, f.path, z.path AS zip_path,
                       EXISTS (SELECT 1 FROM galleries g WHERE g.cover_image_id = i.id) AS is_cover
                FROM images i JOIN files f ON f.id = i.file_id LEFT JOIN files z ON z.id = f.zip_file_id
                WHERE {where} AND i.id > ${len(args) + 1} ORDER BY i.id LIMIT {GENERATE_BATCH}""", *args, after):
        if await ctx.cancelled():
            break
        await asyncio.gather(*(one(r) for r in rows))
        done += len(rows)
        after = rows[-1]["id"]
        await ctx.progress(min(1.0, done / total), f"image thumbnails {done}/{total}")
    return made, failed


SCENE_BATCH = 50      # scenes per generate job when work is split up
IMAGE_BATCH = 500     # images per generate job


async def _enqueue_batches(pool, scene_ids: list[int], image_ids: list[int], force: bool) -> int:
    payloads = [{"scene_ids": scene_ids[i:i + SCENE_BATCH], "image_ids": [], **({"force": True} if force else {})}
                for i in range(0, len(scene_ids), SCENE_BATCH)]
    payloads += [{"scene_ids": [], "image_ids": image_ids[i:i + IMAGE_BATCH]}
                 for i in range(0, len(image_ids), IMAGE_BATCH)]
    async with pool.acquire() as conn, conn.transaction():
        await conn.executemany("INSERT INTO jobs (kind, payload) VALUES ('generate', $1)", [(p,) for p in payloads])
        await conn.execute(f"NOTIFY {jobs.NOTIFY_CHANNEL}")
    return len(payloads)


async def _split_generate(pool, library_id: int | None, force: bool) -> int:
    """Queue the work of a library-wide or global generate as batches. Returns 0 when it is
    small enough for one job to do (then the caller just does it)."""
    cond = "s.library_id = $1" if library_id is not None else "$1::bigint IS NULL"
    scene_ids = [r["id"] for r in await pool.fetch(
        f"""SELECT s.id FROM scenes s JOIN files f ON f.id = s.file_id
            WHERE f.missing_since IS NULL AND {cond} ORDER BY s.id""", library_id)]
    image_ids = [r["id"] for r in await pool.fetch(
        f"""SELECT s.id FROM images s JOIN files f ON f.id = s.file_id LEFT JOIN files z ON z.id = f.zip_file_id
            WHERE f.missing_since IS NULL AND (z.id IS NULL OR z.missing_since IS NULL) AND {cond}
            ORDER BY s.id""", library_id)]
    if len(scene_ids) <= SCENE_BATCH and len(image_ids) <= IMAGE_BATCH:
        return 0
    return await _enqueue_batches(pool, scene_ids, image_ids, force)


async def handle_generate(pool: asyncpg.Pool, settings: Settings, ctx: jobs.JobContext, payload: dict) -> str:
    """Screenshots and scrub sprites for the scenes in scope.

    Default: create what is missing or unusable (empty/unreadable files count as missing),
    so it is idempotent and also repairs. With `force`: discard and rebuild everything in
    scope, and bump each item's asset revision so browsers refetch. Failures are reported
    in the job's message, not just the log."""
    root = settings.generated_dir
    force = bool(payload.get("force"))
    library_id, scene_ids = payload.get("library_id"), payload.get("scene_ids")

    problem = await asyncio.to_thread(generated.check_writable, root)
    if problem:
        raise ScanError(problem)  # fails the job with this explanation

    if force and not scene_ids:
        if library_id is None:
            # Everything: also clears leftovers of files that no longer exist.
            await asyncio.to_thread(generated.wipe_rebuildable, root)
        await _reset_image_thumbnails(pool, root, library_id)

    # A large scope is split into batch jobs, so several can run at once (see the
    # "generate_jobs" setting). The clean-up for a forced rebuild has happened above.
    if "scene_ids" not in payload and "image_ids" not in payload:
        batches = await _split_generate(pool, library_id, force)
        if batches:
            return f"queued {batches} batches"
    elif len(payload.get("scene_ids") or []) > SCENE_BATCH or len(payload.get("image_ids") or []) > IMAGE_BATCH:
        # a big scan: hand its new files out in batches as well
        n = await _enqueue_batches(pool, payload["scene_ids"], payload["image_ids"], force)
        return f"queued {n} batches"

    conds, args = ["true"], []
    if scene_ids is not None:
        args.append(scene_ids)
        conds.append("id = ANY($1::bigint[])")
    elif library_id is not None:
        args.append(library_id)
        conds.append("library_id = $1")
    total = await pool.fetchval(f"SELECT count(*) FROM scenes WHERE {' AND '.join(conds)}", *args)

    sem = asyncio.Semaphore(settings.generate_concurrency)
    made = failed = done = 0
    errors: list[str] = []
    # Identical files share one set of assets (keyed by content hash): build them once.
    locks: dict[str, asyncio.Lock] = {}
    rebuilt: set[str] = set()

    async def one(row):
        nonlocal made, failed
        version = generated.scene_version(row["oshash"], row["file_id"])
        rev = row["assets_rev"] + 1 if force else row["assets_rev"]
        try:
            async with sem, locks.setdefault(version, asyncio.Lock()), crypto.async_private_outputs(row["path"]):
                first = version not in rebuilt
                rebuilt.add(version)
                if row["is_live_photo"]:
                    # The motion of a Live Photo: just a browser-friendly copy, no previews.
                    clip = generated.live_clip_path(root, version)
                    if force and first:
                        await asyncio.to_thread(generated.remove_files, [clip])
                    if not generated.usable(clip):
                        await generated.make_live_clip(row["path"], clip)
                        made += 1
                    return
                if await generated.generate_scene_assets(
                        root, row["path"], row["duration"], version,
                        url_ver=generated.url_version(version, rev), force=force and first):
                    made += 1
                # the card thumbnail, so scene grids are served from a file from the start
                card = generated.thumbnail_path(root, "scenes", version, generated.SCENE_GRID_WIDTH)
                if not generated.usable(card):
                    await asyncio.to_thread(generated.make_thumbnail, generated.screenshot_path(root, version),
                                            generated.SCENE_GRID_WIDTH, card)
        except (ProbeError, OSError) as e:
            failed += 1
            # Don't put file names from users' private libraries into the admin's job list.
            name = f"scene {row['id']}" + ("" if row["library_owner"] is not None
                                           else f" ({os.path.basename(row['path'])})")
            log.warning("generate failed for scene %s: %s", row["id"], e)
            if len(errors) < MAX_REPORTED_ERRORS:
                errors.append(f"{name}: {str(e).splitlines()[-1][:200]}")
        finally:
            if force:
                await pool.execute("UPDATE scenes SET assets_rev = $2 WHERE id = $1", row["id"], rev)

    async for rows in _generate_candidates(pool, payload):
        if await ctx.cancelled():
            break
        await asyncio.gather(*(one(r) for r in rows))
        done += len(rows)
        await ctx.progress(min(1.0, done / max(total, 1)), f"{done}/{total} scenes")

    thumbs, thumb_failed = await _generate_image_thumbnails(pool, settings, ctx, payload)
    summary = f"{made} {'rebuilt' if force else 'generated'}" + (f", {failed} failed" if failed else "")
    if thumbs or thumb_failed:
        summary += f"; {thumbs} image thumbnails" + (f" ({thumb_failed} unreadable)" if thumb_failed else "")
    if errors:
        more = failed - len(errors)
        summary += "\n" + "\n".join(errors) + (f"\n…and {more} more" if more > 0 else "")
    return summary


async def handle_refresh_counts(pool: asyncpg.Pool, settings: Settings, ctx: jobs.JobContext, payload: dict) -> str:
    viewers = await counts.refresh(pool)
    return f"refreshed counts for {viewers} viewer(s)"


async def handle_cleanup(pool: asyncpg.Pool, settings: Settings, ctx: jobs.JobContext, payload: dict) -> str:
    """Forget media that is no longer on disk and sweep the generated files left behind."""
    dry_run = bool(payload.get("dry_run", True))
    scanned = ""
    if payload.get("scan", True):
        # Rescan first: it notices files deleted since the last scan, and follows moved
        # files so that their metadata is kept rather than removed.
        totals = ScanStats()
        scan_payload = {"settle": payload["settle"]} if "settle" in payload else {}
        scanned = await handle_scan(pool, settings, ctx, scan_payload, totals)
        if totals.unsettled:
            # A file that is mid-copy can't be recognised as the new home of a moved file
            # yet, and its old entry (with its metadata) must not be removed meanwhile.
            raise ScanError(f"{totals.unsettled} files changed in the last few seconds and may still be "
                            "copying; nothing was removed. Run the cleanup again in a moment.")
    report = await maintenance.cleanup(pool, settings.generated_dir, ctx, dry_run=dry_run,
                                       min_missing_days=int(payload.get("min_missing_days", 0)))
    removed, swept = report["removed"], report["generated"]
    if not dry_run and (removed["files"] or removed["galleries"]):
        await counts.request_refresh(pool)
    lines = [("would remove " if dry_run else "removed ") + maintenance.describe_removed(removed)
             + " and " + maintenance.describe_swept(swept)]
    if removed["still_on_disk"]:
        lines.append(f"{removed['still_on_disk']} files marked missing are on disk and were kept")
    if removed["libraries_skipped"]:
        lines.append(f"{removed['libraries_skipped']} disabled or unavailable libraries were left alone")
    if scanned:
        lines.append(f"scan: {scanned}")
    return "\n".join(lines)


async def handle_maintenance(pool: asyncpg.Pool, settings: Settings, ctx: jobs.JobContext, payload: dict) -> str:
    report = await maintenance.run_maintenance(pool, settings.generated_dir, ctx)
    if report.get("removed", {}).get("files") or report.get("removed", {}).get("galleries"):
        await counts.request_refresh(pool)
    parts = [f"optimized {report['tables_optimized']} tables",
             f"swept {maintenance.describe_swept(report['generated'])}",
             f"removed {report['sessions_removed']} expired sessions and {report['jobs_removed']} old jobs"]
    if "removed" in report:
        parts.append("forgot " + maintenance.describe_removed(report["removed"]))
    return "; ".join(parts)


async def handle_delete_duplicates(pool: asyncpg.Pool, settings: Settings, ctx: jobs.JobContext, payload: dict) -> str:
    from app.api import duplicates
    try:
        return await duplicates.run_delete_extras(pool, ctx, payload["kind"], bool(payload.get("force")))
    except DeleteError as e:
        raise ScanError(str(e)) from e


async def handle_encrypt_uploads(pool: asyncpg.Pool, settings: Settings, ctx: jobs.JobContext, payload: dict) -> str:
    """Encrypt users' uploads (and what was generated from them) that aren't yet: run when
    encryption is turned on after files were uploaded, and harmless to run again."""
    if not crypto.enabled():
        return "encryption of user uploads is off (no key): nothing to do"
    root = settings.generated_dir
    rows = await pool.fetch(
        """SELECT f.id, f.path, f.oshash, f.mod_time, s.id AS scene_id, i.id AS image_id
           FROM files f JOIN library_paths l ON l.id = f.library_id
           LEFT JOIN scenes s ON s.file_id = f.id LEFT JOIN images i ON i.file_id = f.id
           WHERE l.owner_id IS NOT NULL AND f.zip_file_id IS NULL AND f.missing_since IS NULL
           ORDER BY f.id""")
    members = await pool.fetch(
        """SELECT m.id, m.mod_time FROM files m JOIN files z ON z.id = m.zip_file_id
           JOIN library_paths l ON l.id = z.library_id JOIN images i ON i.file_id = m.id
           WHERE l.owner_id IS NOT NULL""")
    done = files = assets = failed = 0
    for row in rows:
        if await ctx.cancelled():
            break
        try:
            if await asyncio.to_thread(crypto.encrypt_file_in_place, row["path"]):
                files += 1
        except OSError as e:
            failed += 1
            log.warning("could not encrypt %s: %s", row["path"], e)
        derived = []
        if row["scene_id"]:
            version = generated.scene_version(row["oshash"], row["id"])
            derived = generated.scene_asset_files(root, version)
        elif row["image_id"]:
            derived = generated.image_asset_files(root, generated.image_version(row["id"], row["mod_time"]))
        assets += await asyncio.to_thread(_encrypt_existing, derived)
        done += 1
        await ctx.progress(done / max(len(rows), 1), f"{done} of {len(rows)} uploaded files")
    for m in members:                                   # thumbnails of images inside uploaded zips
        assets += await asyncio.to_thread(
            _encrypt_existing, generated.image_asset_files(root, generated.image_version(m["id"], m["mod_time"])))
    message = f"encrypted {files} uploaded files and {assets} generated files"
    return message + (f"; {failed} could not be encrypted (see the worker log)" if failed else "")


def _encrypt_existing(paths) -> int:
    n = 0
    for p in paths:
        try:
            if p.exists() and crypto.encrypt_file_in_place(p):
                n += 1
        except OSError as e:
            log.warning("could not encrypt %s: %s", p, e)
    return n


async def handle_folder_link_backfill(pool: asyncpg.Pool, settings: Settings, ctx: jobs.JobContext, payload: dict) -> str:
    return await folder_links.backfill(pool, ctx, payload["link_id"])


async def handle_folder_link_remove(pool: asyncpg.Pool, settings: Settings, ctx: jobs.JobContext, payload: dict) -> str:
    return await folder_links.remove_tags(pool, ctx, payload["link_id"])


HANDLERS = {folder_links.JOB_KIND: handle_folder_link_backfill, folder_links.REMOVE_JOB_KIND: handle_folder_link_remove, "encrypt_uploads": handle_encrypt_uploads, "delete_duplicates": handle_delete_duplicates, "scan": handle_scan, "generate": handle_generate, counts.JOB_KIND: handle_refresh_counts,
            maintenance.CLEANUP_KIND: handle_cleanup, maintenance.JOB_KIND: handle_maintenance}

SCHEDULE_CHECK_SECONDS = 60


async def schedule_loop(pool: asyncpg.Pool, stop: asyncio.Event) -> None:
    """Queue the daily maintenance job when it is due. This loop only ever enqueues that
    one job, at most once per day, and the job itself enqueues nothing but a counts refresh."""
    while not stop.is_set():
        try:
            await maintenance.enqueue_if_due(pool)
        except (OSError, asyncpg.PostgresError):
            log.exception("maintenance schedule check failed")
        try:
            await asyncio.wait_for(stop.wait(), SCHEDULE_CHECK_SECONDS)
        except TimeoutError:
            pass


async def execute(pool: asyncpg.Pool, settings: Settings, job: asyncpg.Record) -> None:
    handler = HANDLERS.get(job["kind"])
    log.info("job %s (%s) started", job["id"], job["kind"])
    if handler is None:
        await jobs.fail(pool, job["id"], f"unknown job kind {job['kind']!r}")
        return
    ctx = jobs.JobContext(pool, job["id"])
    try:
        message = await handler(pool, settings, ctx, job["payload"])
    except (ScanError, OSError) as e:
        await jobs.fail(pool, job["id"], str(e))
        log.warning("job %s failed: %s", job["id"], e)
    except Exception:
        await jobs.fail(pool, job["id"], traceback.format_exc(limit=5))
        log.exception("job %s crashed", job["id"])
    else:
        await jobs.finish(pool, job["id"], message)
        log.info("job %s finished: %s", job["id"], message)


async def run_one(pool: asyncpg.Pool, settings: Settings) -> bool:
    """Claim and run a single job, of any kind. Returns False if nothing was runnable."""
    job = await jobs.claim(pool)
    if job is None:
        return False
    await execute(pool, settings, job)
    return True


# How jobs share the worker:
# - generate jobs (previews, thumbnails) may run several at once (the admin setting);
# - refresh_counts is a short database job that runs alongside anything;
# - everything else (scans, cleanup, maintenance, duplicate removal) reads the disk hard
#   and runs alone. A waiting one also goes first: no new generate job starts until it has
#   had its turn, so new files get indexed promptly even during a long preview backlog.
PARALLEL = "generate"
LIGHT = counts.JOB_KIND
WORKER_SETTINGS = "worker_settings"
MAX_GENERATE_JOBS = 16


async def generate_jobs(pool: asyncpg.Pool, settings: Settings) -> int:
    stored = await maintenance.get_state(pool, WORKER_SETTINGS, {})
    return max(1, min(MAX_GENERATE_JOBS, int(stored.get("generate_jobs", settings.generate_jobs))))


async def claim_next(pool: asyncpg.Pool, settings: Settings, running: list[str]) -> asyncpg.Record | None:
    """The next job that may start alongside those `running` (their kinds), if any."""
    if LIGHT not in running and (job := await jobs.claim(pool, kinds=[LIGHT])):
        return job
    if any(k not in (PARALLEL, LIGHT) for k in running):
        return None                                   # something exclusive is running
    gen_running = running.count(PARALLEL)
    if await jobs.runnable_exists(pool, exclude=[PARALLEL, LIGHT]):
        # an exclusive job is waiting: let the generate jobs finish, then run it alone
        return await jobs.claim(pool, exclude=[PARALLEL, LIGHT]) if gen_running == 0 else None
    if gen_running < await generate_jobs(pool, settings):
        return await jobs.claim(pool, kinds=[PARALLEL])
    return None


async def job_loop(pool, settings, wake: asyncio.Event, stop: asyncio.Event) -> None:
    running: dict[asyncio.Task, str] = {}

    def done(task: asyncio.Task) -> None:
        running.pop(task, None)
        wake.set()

    while not stop.is_set():
        wake.clear()
        try:
            while job := await claim_next(pool, settings, list(running.values())):
                task = asyncio.create_task(execute(pool, settings, job))
                running[task] = job["kind"]
                task.add_done_callback(done)
        except (OSError, asyncpg.PostgresError):
            log.exception("job loop database error; retrying")
        # Sleep until a job finishes, a NOTIFY arrives, stop, or a periodic check for
        # delayed jobs.
        waiters = [asyncio.ensure_future(wake.wait()), asyncio.ensure_future(stop.wait())]
        await asyncio.wait(waiters, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        for w in waiters:
            w.cancel()
    # Shutting down: give running jobs a moment; anything unfinished is picked up again
    # (recover_stale) when the worker restarts.
    if running:
        await asyncio.wait(list(running), timeout=20)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    crypto.load_key(settings.uploads_key_file)   # a malformed key stops the worker here, loudly
    # Background work yields to everything else on the machine, and never uses more
    # threads for file access than the configured concurrency allows.
    try:
        os.nice(10)
    except OSError:
        pass
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=settings.scan_probe_concurrency + 8, thread_name_prefix="io"))
    settings.generated_dir.mkdir(parents=True, exist_ok=True)
    pool = await create_pool(settings.db_params(), 2, settings.scan_probe_concurrency + 4 + MAX_GENERATE_JOBS)
    await migrate(pool)
    await jobs.recover_stale(pool)
    await jobs.prune(pool)
    await counts.request_refresh(pool)  # cheap; guarantees the cache exists after upgrades
    if crypto.enabled():
        # Encryption may have just been turned on: encrypt whatever was uploaded before.
        await jobs.enqueue(pool, "encrypt_uploads", {}, dedupe_key="encrypt_uploads")

    stop, wake = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    watcher = LibraryWatcher(pool, polling=settings.watcher_polling,
                             quiet_seconds=settings.watcher_quiet_seconds,
                             max_wait_seconds=settings.watcher_max_wait_seconds)
    reload_requested = asyncio.Event()
    reload_requested.set()  # initial watcher setup runs inside reload_loop, off the job loop's path
    listener = await asyncpg.connect(**settings.db_params())
    await listener.add_listener(jobs.NOTIFY_CHANNEL, lambda *_: wake.set())
    await listener.add_listener(LIBRARIES_CHANNEL, lambda *_: reload_requested.set())

    async def reload_loop():
        while not stop.is_set():
            waiters = [asyncio.ensure_future(reload_requested.wait()), asyncio.ensure_future(stop.wait())]
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for w in waiters:
                w.cancel()
            if reload_requested.is_set() and not stop.is_set():
                reload_requested.clear()
                try:
                    await watcher.reload()
                except Exception:
                    log.exception("watcher reload failed")

    log.info("worker ready")
    try:
        await asyncio.gather(job_loop(pool, settings, wake, stop), watcher.run(stop), reload_loop(),
                             schedule_loop(pool, stop))
    finally:
        watcher.stop()
        await listener.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
