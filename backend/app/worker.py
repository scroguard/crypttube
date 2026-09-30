"""Background worker: runs queued jobs one at a time and hosts the filesystem watcher.

Run as its own container (`python -m app.worker`) so scans and ffmpeg work never
compete with the API process for the event loop.
"""

import asyncio
import logging
import signal
import traceback

import asyncpg

from app import generated, jobs
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
        scanner = Scanner(pool, settings.generated_dir, ctx=ctx,
                          settle_seconds=settings.scan_settle_seconds,
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

    # Scan -> generate is the only direction: generation never enqueues scans.
    scene_ids = [i for s in results for i in s.scene_ids]
    if scene_ids:
        await jobs.enqueue(pool, "generate", {"scene_ids": scene_ids})

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


async def _generate_candidates(pool: asyncpg.Pool, payload: dict):
    """Yield batches of scenes to generate for: explicit ids, or every scene (paged by id)."""
    query = """SELECT s.id, s.duration, f.id AS file_id, f.path, f.oshash
               FROM scenes s JOIN files f ON f.id = s.file_id
               WHERE f.missing_since IS NULL AND {cond} ORDER BY s.id LIMIT $2"""
    if payload.get("scene_ids"):
        ids = payload["scene_ids"]
        for i in range(0, len(ids), GENERATE_BATCH):
            yield await pool.fetch(query.format(cond="s.id = ANY($1)"), ids[i:i + GENERATE_BATCH], GENERATE_BATCH)
        return
    after = 0
    while rows := await pool.fetch(query.format(cond="s.id > $1"), after, GENERATE_BATCH):
        yield rows
        after = rows[-1]["id"]


async def handle_generate(pool: asyncpg.Pool, settings: Settings, ctx: jobs.JobContext, payload: dict) -> str:
    """Screenshots and scrub sprites. Existing assets are skipped, so this is idempotent."""
    total = len(payload["scene_ids"]) if payload.get("scene_ids") else await pool.fetchval(
        "SELECT count(*) FROM scenes")
    sem = asyncio.Semaphore(settings.generate_concurrency)
    made = failed = done = 0

    async def one(row):
        nonlocal made, failed
        version = generated.scene_version(row["oshash"], row["file_id"])
        async with sem:
            try:
                if await generated.generate_scene_assets(settings.generated_dir, row["path"],
                                                         row["duration"], version):
                    made += 1
            except (ProbeError, OSError) as e:
                failed += 1
                log.warning("generate failed for scene %s: %s", row["id"], e)

    async for rows in _generate_candidates(pool, payload):
        if await ctx.cancelled():
            break
        await asyncio.gather(*(one(r) for r in rows))
        done += len(rows)
        await ctx.progress(min(1.0, done / max(total, 1)), f"{done}/{total} scenes")
    return f"{made} generated" + (f", {failed} failed" if failed else "")


HANDLERS = {"scan": handle_scan, "generate": handle_generate}


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
    pool = await create_pool(settings.database_url, 2, settings.scan_probe_concurrency + 4)
    await migrate(pool)
    await jobs.recover_stale(pool)
    await jobs.prune(pool)

    stop, wake = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    watcher = LibraryWatcher(pool, polling=settings.watcher_polling,
                             quiet_seconds=settings.watcher_quiet_seconds,
                             max_wait_seconds=settings.watcher_max_wait_seconds)
    await watcher.reload()

    reload_requested = asyncio.Event()
    listener = await asyncpg.connect(settings.database_url)
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
                await watcher.reload()

    log.info("worker ready")
    try:
        await asyncio.gather(job_loop(pool, settings, wake, stop), watcher.run(stop), reload_loop())
    finally:
        watcher.stop()
        await listener.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
