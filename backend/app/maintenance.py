"""Housekeeping: forget media that is gone, sweep generated files nothing refers to, and
keep the database in shape.

Two jobs use this (see app.worker):

- `cleanup` (run by an admin): rescans, then removes the database entries of files that
  are no longer on disk and the generated files left behind. With `dry_run` it only
  reports what it would do.
- `maintenance` (daily, or on request): prunes expired sessions and old jobs, sweeps
  orphaned generated files, optionally forgets files that have been missing for a
  configured number of days, then VACUUM ANALYZEs every table.

Originals are never touched: the only files deleted are under the generated volume.
Reports contain counts only, so they reveal nothing about users' private libraries.
"""

import asyncio
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

from app import generated, jobs

SETTINGS_KEY = "maintenance_settings"
SCHEDULE_KEY = "maintenance_schedule"
MAINTENANCE_REPORT = "maintenance_report"
CLEANUP_REPORT = "cleanup_report"
JOB_KIND = "maintenance"
CLEANUP_KIND = "cleanup"

DEFAULT_SETTINGS = {
    "enabled": True,
    "hour": 4,                        # local time of the server (TZ)
    "remove_missing_after_days": 0,   # 0 = never forget missing files automatically
}
SWEEP_BATCH = 2000
TEMP_MAX_AGE = 6 * 3600               # leftovers of interrupted writes/uploads
ENTITY_KINDS = ("performers", "studios", "tags")
JOBS_KEEP_DAYS = 7
FAILED_JOBS_KEEP_DAYS = 30


# ---------------------------------------------------------------------------
# Stored state
# ---------------------------------------------------------------------------

async def get_state(db, key: str, default=None):
    value = await db.fetchval("SELECT value FROM system_state WHERE key = $1", key)
    return default if value is None else value


async def set_state(db, key: str, value) -> None:
    await db.execute(
        """INSERT INTO system_state (key, value) VALUES ($1, $2)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""", key, value)


async def get_settings(db) -> dict:
    return DEFAULT_SETTINGS | await get_state(db, SETTINGS_KEY, {})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def is_due(now: datetime, hour: int, last: datetime | None) -> bool:
    """True if the most recent daily slot (`hour`:00 in `now`'s timezone) has not been
    served yet. A worker that was down at that hour catches up when it starts."""
    slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=1)
    return last is None or last < slot


async def enqueue_if_due(pool: asyncpg.Pool, now: datetime | None = None) -> bool:
    """Queue the daily maintenance job when its time has come. At most one job per day:
    the slot is recorded as served before the job runs, whatever its outcome."""
    cfg = await get_settings(pool)
    if not cfg["enabled"]:
        return False
    now = now or datetime.now().astimezone()
    last = (await get_state(pool, SCHEDULE_KEY, {})).get("last_enqueued")
    if not is_due(now, cfg["hour"], datetime.fromisoformat(last) if last else None):
        return False
    await set_state(pool, SCHEDULE_KEY, {"last_enqueued": now.isoformat()})
    await jobs.enqueue(pool, JOB_KIND, {}, dedupe_key=JOB_KIND)
    return True


# ---------------------------------------------------------------------------
# Forgetting files that are gone
# ---------------------------------------------------------------------------

def _present(paths: list[str]) -> list[bool]:
    return [os.path.lexists(p) for p in paths]


def _usable_dirs(paths: list[str]) -> list[bool]:
    return [os.path.isdir(p) for p in paths]


async def remove_missing(conn: asyncpg.Connection, min_days: int = 0) -> dict:
    """Delete the entries of files marked missing for at least `min_days` (and with them
    their scenes, images and zip galleries), plus folder galleries whose folder is gone.

    Guards: a library that is disabled or whose folder is not there right now (an
    unmounted drive) is skipped entirely, and a file that is in fact on disk is kept.
    Run inside a transaction; rolling it back makes this a dry run."""
    libs = await conn.fetch("SELECT id, path, enabled FROM library_paths ORDER BY id")
    there = await asyncio.to_thread(_usable_dirs, [lib["path"] for lib in libs])
    usable = [lib["id"] for lib, ok in zip(libs, there) if ok and lib["enabled"]]
    result = {"scenes": 0, "images": 0, "galleries": 0, "files": 0, "still_on_disk": 0,
              "libraries_skipped": len(libs) - len(usable)}

    rows = await conn.fetch(
        """SELECT f.id, f.path, f.zip_file_id FROM files f
           WHERE f.missing_since IS NOT NULL AND f.missing_since <= now() - make_interval(days => $1)
             AND f.library_id = ANY($2::bigint[])""", min_days, usable)
    plain = [r for r in rows if r["zip_file_id"] is None]
    on_disk = await asyncio.to_thread(_present, [r["path"] for r in plain])
    kept = {r["id"] for r, present in zip(plain, on_disk) if present}
    result["still_on_disk"] = len(kept)
    gone = [r["id"] for r in rows if r["id"] not in kept]
    if gone:
        result["scenes"] = await conn.fetchval("SELECT count(*) FROM scenes WHERE file_id = ANY($1::bigint[])", gone)
        result["images"] = await conn.fetchval(
            """SELECT count(*) FROM images i JOIN files f ON f.id = i.file_id
               WHERE f.id = ANY($1::bigint[]) OR f.zip_file_id = ANY($1::bigint[])""", gone)
        result["galleries"] = await conn.fetchval(
            "SELECT count(*) FROM galleries WHERE zip_file_id = ANY($1::bigint[])", gone)
        result["files"] = len(gone)
        await conn.execute("DELETE FROM files WHERE id = ANY($1::bigint[])", gone)   # cascades

    folders = await conn.fetch(
        """SELECT id, folder_path FROM galleries
           WHERE folder_path IS NOT NULL AND image_count = 0 AND library_id = ANY($1::bigint[])""", usable)
    there = await asyncio.to_thread(_usable_dirs, [g["folder_path"] for g in folders])
    empty = [g["id"] for g, ok in zip(folders, there) if not ok]
    if empty:
        await conn.execute("DELETE FROM galleries WHERE id = ANY($1::bigint[])", empty)
        result["galleries"] += len(empty)
    return result


# ---------------------------------------------------------------------------
# Sweeping generated files nothing refers to
# ---------------------------------------------------------------------------

def _list_dir(path: Path) -> list[tuple[str, int, float, bool]]:
    """(name, size, mtime, is_dir) of a directory's entries; [] if it isn't there."""
    out = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    out.append((e.name, st.st_size, st.st_mtime, e.is_dir(follow_symlinks=False)))
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _unlink(paths: list[Path]) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _is_temp(name: str) -> bool:
    return name.startswith((".tmp-", ".write-test-"))


async def _valid_scene_keys(db, keys: set[str]) -> set[str]:
    """Scene assets are keyed by the file's content hash (or f<file id> without one)."""
    rows = await db.fetch(
        """SELECT k FROM unnest($1::text[]) k
           WHERE EXISTS (SELECT 1 FROM files f JOIN scenes s ON s.file_id = f.id WHERE f.oshash = k)
              OR (k ~ '^f[0-9]{1,18}$' AND EXISTS (
                    SELECT 1 FROM files f JOIN scenes s ON s.file_id = f.id
                    WHERE f.id = substr(k, 2)::bigint AND f.oshash IS NULL))""", list(keys))
    return {r["k"] for r in rows}


async def _valid_image_keys(db, keys: set[str]) -> set[str]:
    """Image thumbnails are keyed by file id + modification time."""
    ids = {int(k.split("-", 1)[0]) for k in keys if re.fullmatch(r"\d{1,18}-[0-9a-f]+", k)}
    rows = await db.fetch(
        "SELECT f.id, f.mod_time FROM files f JOIN images i ON i.file_id = f.id WHERE f.id = ANY($1::bigint[])",
        list(ids))
    return {generated.image_version(r["id"], r["mod_time"]) for r in rows}


class _Tally:
    def __init__(self) -> None:
        self.files = self.bytes = 0

    def add(self, size: int) -> None:
        self.files += 1
        self.bytes += size

    def as_dict(self) -> dict:
        return {"files": self.files, "bytes": self.bytes}


async def _sweep_keyed(db, base: Path, key_of, valid_keys, delete: bool, tally: _Tally, temp: _Tally,
                       ctx: jobs.JobContext | None) -> None:
    """One asset directory (base/<shard>/<key><suffix>): delete files whose key has no source."""
    cutoff = time.time() - TEMP_MAX_AGE

    async def flush(batch: list[tuple[Path, str, int]]) -> None:
        valid = await valid_keys(db, {key for _, key, _ in batch})
        orphans = [(p, size) for p, key, size in batch if key not in valid]
        for _, size in orphans:
            tally.add(size)
        if delete:
            await asyncio.to_thread(_unlink, [p for p, _ in orphans])

    batch: list[tuple[Path, str, int]] = []
    shards: list[Path] = []
    for shard, _, _, is_dir in sorted(await asyncio.to_thread(_list_dir, base)):
        if not is_dir or (ctx and await ctx.cancelled()):
            continue
        stale = []
        for name, size, mtime, sub in await asyncio.to_thread(_list_dir, base / shard):
            if sub:
                continue
            if _is_temp(name):
                if mtime < cutoff:
                    temp.add(size)
                    stale.append(base / shard / name)
                continue
            key = key_of(name)
            if key is not None:
                batch.append((base / shard / name, key, size))
        if delete and stale:
            await asyncio.to_thread(_unlink, stale)
        shards.append(base / shard)
        if len(batch) >= SWEEP_BATCH:
            await flush(batch)
            batch = []
    if batch:
        await flush(batch)
    if delete:
        for path in shards:
            await asyncio.to_thread(_rmdir_if_empty, path)


def _plain_key(name: str) -> str | None:
    stem, ext = os.path.splitext(name)
    return stem if ext in (".webp", ".vtt") else None


def _thumb_key(name: str) -> str | None:
    m = re.fullmatch(r"(.+)-w\d+\.webp", name)
    return m.group(1) if m else None


async def sweep_generated(db, root: Path, *, delete: bool, ctx: jobs.JobContext | None = None) -> dict:
    """Find (and with `delete`, remove) generated files whose source no longer exists:
    scene screenshots/sprites/thumbnails, image thumbnails, pictures of deleted
    performers/studios/tags, and stale temporary files. Also clears picture references
    whose file has vanished. Files this app did not create are left alone."""
    scene, image, entity, temp = _Tally(), _Tally(), _Tally(), _Tally()
    for sub, key_of in (("screenshots", _plain_key), ("sprites", _plain_key), ("thumbs/scenes", _thumb_key)):
        await _sweep_keyed(db, root / sub, key_of, _valid_scene_keys, delete, scene, temp, ctx)
    await _sweep_keyed(db, root / "thumbs/images", _thumb_key, _valid_image_keys, delete, image, temp, ctx)
    await _sweep_keyed(db, root / "display", _plain_key, _valid_image_keys, delete, image, temp, ctx)

    cutoff = time.time() - TEMP_MAX_AGE
    links_fixed = 0
    for kind in ENTITY_KINDS:
        entries = await asyncio.to_thread(_list_dir, root / "entities" / kind)
        referenced = {r["image_path"] for r in await db.fetch(
            f"SELECT image_path FROM {kind} WHERE image_path IS NOT NULL")}
        doomed = []
        for name, size, mtime, is_dir in entries:
            if is_dir:
                continue
            if _is_temp(name):
                if mtime < cutoff:
                    temp.add(size)
                    doomed.append(root / "entities" / kind / name)
            elif f"entities/{kind}/{name}" not in referenced:
                entity.add(size)
                doomed.append(root / "entities" / kind / name)
        if delete:
            await asyncio.to_thread(_unlink, doomed)
        # The other direction: a row pointing at a picture that isn't there.
        broken = sorted(referenced - {f"entities/{kind}/{name}" for name, *_ in entries})
        links_fixed += len(broken)
        if delete and broken:
            await db.execute(f"UPDATE {kind} SET image_path = NULL WHERE image_path = ANY($1::text[])", broken)

    doomed = []
    for name, size, mtime, is_dir in await asyncio.to_thread(_list_dir, root / "tmp"):
        if not is_dir and mtime < cutoff:
            temp.add(size)
            doomed.append(root / "tmp" / name)
    if delete:
        await asyncio.to_thread(_unlink, doomed)

    parts = {"scene_assets": scene, "image_thumbnails": image, "entity_images": entity, "temporary": temp}
    return {**{k: t.as_dict() for k, t in parts.items()},
            "files": sum(t.files for t in parts.values()), "bytes": sum(t.bytes for t in parts.values()),
            "links_fixed": links_fixed}


# ---------------------------------------------------------------------------
# The two jobs
# ---------------------------------------------------------------------------

async def cleanup(pool: asyncpg.Pool, root: Path, ctx: jobs.JobContext | None, *, dry_run: bool,
                  min_missing_days: int = 0) -> dict:
    """Forget missing files and sweep what they leave behind. A dry run does the same
    work inside a transaction that is rolled back, and deletes no files."""
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            removed = await remove_missing(conn, min_missing_days)
            if dry_run:
                swept = await sweep_generated(conn, root, delete=False, ctx=ctx)
        finally:
            if dry_run:
                await tx.rollback()
            else:
                await tx.commit()
    if not dry_run:
        # Only after the removal is committed: never delete the assets of rows that still exist.
        swept = await sweep_generated(pool, root, delete=True, ctx=ctx)
    report = {"at": _now(), "dry_run": dry_run, "min_missing_days": min_missing_days,
              "removed": removed, "generated": swept}
    await set_state(pool, CLEANUP_REPORT, report)
    return report


def _affected(status: str) -> int:
    """Row count from a command tag such as 'DELETE 12'."""
    return int(status.rsplit(" ", 1)[-1])


async def run_maintenance(pool: asyncpg.Pool, root: Path, ctx: jobs.JobContext | None = None) -> dict:
    started = time.monotonic()
    cfg = await get_settings(pool)
    size_before = await pool.fetchval("SELECT pg_database_size(current_database())")
    report: dict = {"at": _now()}

    report["sessions_removed"] = _affected(await pool.execute("DELETE FROM sessions WHERE expires_at < now()"))
    report["jobs_removed"] = _affected(await pool.execute(
        """DELETE FROM jobs WHERE (status IN ('done', 'cancelled') AND finished_at < now() - make_interval(days => $1))
                              OR (status = 'failed' AND finished_at < now() - make_interval(days => $2))""",
        JOBS_KEEP_DAYS, FAILED_JOBS_KEEP_DAYS))

    days = cfg["remove_missing_after_days"]
    if days > 0:
        async with pool.acquire() as conn, conn.transaction():
            report["removed"] = await remove_missing(conn, days)
    report["generated"] = await sweep_generated(pool, root, delete=True, ctx=ctx)

    # VACUUM reclaims dead rows and keeps index-only scans fast; ANALYZE refreshes the
    # planner statistics the list queries depend on. Neither blocks readers or writers.
    tables = [r["name"] for r in await pool.fetch(
        "SELECT quote_ident(relname) AS name FROM pg_stat_user_tables WHERE schemaname = 'public' ORDER BY relname")]
    done = 0
    for done, table in enumerate(tables, start=1):
        if ctx and await ctx.cancelled():
            done -= 1
            break
        await pool.execute(f"VACUUM (ANALYZE) {table}")
        if ctx:
            await ctx.progress(done / len(tables), f"optimizing tables ({done}/{len(tables)})")
    report["tables_optimized"] = done
    report["database_bytes_before"] = size_before
    report["database_bytes"] = await pool.fetchval("SELECT pg_database_size(current_database())")
    report["seconds"] = round(time.monotonic() - started, 1)
    await set_state(pool, MAINTENANCE_REPORT, report)
    return report


def describe_removed(removed: dict) -> str:
    return (f"{removed['scenes']} scenes, {removed['images']} images, {removed['galleries']} galleries")


def describe_swept(swept: dict) -> str:
    return f"{swept['files']} generated files ({swept['bytes'] / 1_048_576:.1f} MB)"
