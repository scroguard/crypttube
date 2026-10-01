"""Background worker: runs queued jobs one at a time and hosts the filesystem watcher.

Run as its own container (`python -m app.worker`) so scans and ffmpeg work never
compete with the API process for the event loop.
"""

import asyncio
import logging
import os
import signal
import traceback

import asyncpg

from app import counts, generated, jobs
from app.config import Settings, get_settings
from app.db import create_pool, migrate
from app.media import ProbeError
from app.scanner import ScanError, Scanner, ScanStats
from app.watcher import LibraryWatcher, scan_dedupe_key

log = logging.getLogger("worker")

LIBRARIES_CHANNEL = "libraries_changed"


async def handle_scan(pool: asyncpg.Pool, settings: Settings, ctx: jobs.JobContext, payload: dict) -> str:
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
            if len(library_ids) == 1:
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
    if scene_ids:
        await jobs.enqueue(pool, "generate", {"scene_ids": scene_ids})
    if any(s.summary() != "no changes" for s in results):
        await counts.request_refresh(pool)

    total = ScanStats()
    for s in results:
        for k, v in vars(s).items():
            if isinstance(v, int):
                setattr(total, k, getattr(total, k) + v)
    message = total.summary()
    if unavailable:
        message += "; skipped: " + "; ".join(unavailable)
    return message


GENERATE_BATCH = 200
MAX_REPORTED_ERRORS = 10


async def _generate_candidates(pool: asyncpg.Pool, payload: dict):
    """Yield batches of scenes in scope: explicit ids, one library, or all (paged by id)."""
    conds, args = ["f.missing_since IS NULL"], []
    if payload.get("scene_ids"):
        args.append(payload["scene_ids"])
        conds.append(f"s.id = ANY(${len(args)}::bigint[])")
    if payload.get("library_id") is not None:
        args.append(payload["library_id"])
        conds.append(f"s.library_id = ${len(args)}")
    query = f"""SELECT s.id, s.duration, s.assets_rev, f.id AS file_id, f.path, f.oshash,
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

    conds, args = ["true"], []
    if scene_ids:
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
            async with sem, locks.setdefault(version, asyncio.Lock()):
                first = version not in rebuilt
                rebuilt.add(version)
                if await generated.generate_scene_assets(
                        root, row["path"], row["duration"], version,
                        url_ver=generated.url_version(version, rev), force=force and first):
                    made += 1
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

    summary = f"{made} {'rebuilt' if force else 'generated'}" + (f", {failed} failed" if failed else "")
    if errors:
        more = failed - len(errors)
        summary += "\n" + "\n".join(errors) + (f"\n…and {more} more" if more > 0 else "")
    return summary


async def handle_refresh_counts(pool: asyncpg.Pool, settings: Settings, ctx: jobs.JobContext, payload: dict) -> str:
    viewers = await counts.refresh(pool)
    return f"refreshed counts for {viewers} viewer(s)"


HANDLERS = {"scan": handle_scan, "generate": handle_generate, counts.JOB_KIND: handle_refresh_counts}


async def run_one(pool: asyncpg.Pool, settings: Settings) -> bool:
    """Claim and run a single job. Returns False if the queue had nothing runnable."""
    job = await jobs.claim(pool)
    if job is None:
        return False
    handler = HANDLERS.get(job["kind"])
    log.info("job %s (%s) started", job["id"], job["kind"])
    if handler is None:
        await jobs.fail(pool, job["id"], f"unknown job kind {job['kind']!r}")
        return True
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
    return True


async def job_loop(pool, settings, wake: asyncio.Event, stop: asyncio.Event) -> None:
    while not stop.is_set():
        wake.clear()
        try:
            if await run_one(pool, settings):
                continue
        except (OSError, asyncpg.PostgresConnectionError):
            log.exception("job loop database error; retrying")
        # Nothing runnable: sleep until NOTIFY, stop, or a periodic check for delayed jobs.
        waiters = [asyncio.ensure_future(wake.wait()), asyncio.ensure_future(stop.wait())]
        await asyncio.wait(waiters, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        for w in waiters:
            w.cancel()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    settings.generated_dir.mkdir(parents=True, exist_ok=True)
    pool = await create_pool(settings.db_params(), 2, settings.scan_probe_concurrency + 4)
    await migrate(pool)
    await jobs.recover_stale(pool)
    await jobs.prune(pool)
    await counts.request_refresh(pool)  # cheap; guarantees the cache exists after upgrades

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
        await asyncio.gather(job_loop(pool, settings, wake, stop), watcher.run(stop), reload_loop())
    finally:
        watcher.stop()
        await listener.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
