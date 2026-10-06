"""Postgres-backed job queue (SELECT ... FOR UPDATE SKIP LOCKED + LISTEN/NOTIFY wakeups)."""

import time
from datetime import datetime, timedelta, timezone

import asyncpg

NOTIFY_CHANNEL = "jobs"
MAX_ATTEMPTS = 3


async def enqueue(
    db: asyncpg.Pool | asyncpg.Connection,
    kind: str,
    payload: dict | None = None,
    *,
    dedupe_key: str | None = None,
    delay: float = 0,
) -> int:
    """Queue a job. If a queued job with the same dedupe_key exists, it absorbs this
    request (keeping the earliest run time) and its id is returned instead."""
    run_after = datetime.now(timezone.utc) + timedelta(seconds=delay)
    job_id = await db.fetchval(
        """INSERT INTO jobs (kind, payload, dedupe_key, run_after) VALUES ($1, $2, $3, $4)
           ON CONFLICT (dedupe_key) WHERE status = 'queued'
           DO UPDATE SET run_after = LEAST(jobs.run_after, EXCLUDED.run_after)
           RETURNING id""",
        kind, payload or {}, dedupe_key, run_after,
    )
    await db.execute(f"NOTIFY {NOTIFY_CHANNEL}")
    return job_id


async def claim(db: asyncpg.Pool | asyncpg.Connection, *, kinds: list[str] | None = None,
                exclude: list[str] | None = None) -> asyncpg.Record | None:
    """Atomically take the oldest runnable job (optionally only of `kinds`, or of any kind
    but `exclude`). Jobs whose dedupe_key is already running wait, so the same scan
    never runs twice concurrently."""
    return await db.fetchrow(
        """UPDATE jobs SET status = 'running', started_at = now(), attempts = attempts + 1
           WHERE id = (
               SELECT j.id FROM jobs j
               WHERE j.status = 'queued' AND j.run_after <= now()
                 AND ($1::text[] IS NULL OR j.kind = ANY($1::text[]))
                 AND ($2::text[] IS NULL OR j.kind <> ALL($2::text[]))
                 AND (j.dedupe_key IS NULL OR NOT EXISTS (
                     SELECT 1 FROM jobs r WHERE r.status = 'running' AND r.dedupe_key = j.dedupe_key))
               ORDER BY j.run_after, j.id
               FOR UPDATE SKIP LOCKED
               LIMIT 1)
           RETURNING id, kind, payload, attempts""",
        kinds, exclude,
    )


async def runnable_exists(db, *, exclude: list[str]) -> bool:
    """Is a job of some other kind than `exclude` waiting to run right now?"""
    return await db.fetchval(
        """SELECT EXISTS (SELECT 1 FROM jobs WHERE status = 'queued' AND run_after <= now()
                          AND kind <> ALL($1::text[]))""", exclude)


async def finish(db, job_id: int, message: str | None = None) -> None:
    await db.execute(
        """UPDATE jobs SET status = CASE WHEN cancel_requested THEN 'cancelled'::job_status ELSE 'done' END,
                           progress = 1, message = $2, finished_at = now()
           WHERE id = $1""",
        job_id, message,
    )


async def fail(db, job_id: int, error: str) -> None:
    await db.execute(
        "UPDATE jobs SET status = 'failed', error = $2, finished_at = now() WHERE id = $1",
        job_id, error,
    )


async def cancel(db, job_id: int) -> bool:
    """Cancel a queued job immediately, or flag a running one to stop."""
    status = await db.fetchval(
        """UPDATE jobs SET
               status = CASE WHEN status = 'queued' THEN 'cancelled'::job_status ELSE status END,
               finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END,
               cancel_requested = true
           WHERE id = $1 AND status IN ('queued', 'running')
           RETURNING status""",
        job_id,
    )
    return status is not None


async def recover_stale(db) -> None:
    """On worker start, anything still 'running' was interrupted. Retry it, unless it
    has already been attempted MAX_ATTEMPTS times (then it is probably what kills us)."""
    await db.execute(
        """UPDATE jobs SET status = 'failed', error = 'interrupted too many times', finished_at = now()
           WHERE status = 'running' AND attempts >= $1""",
        MAX_ATTEMPTS,
    )
    # A requeued job may collide with a newer queued job for the same key; drop ours then.
    await db.execute(
        """UPDATE jobs SET status = 'cancelled', finished_at = now(), message = 'superseded'
           WHERE status = 'running' AND dedupe_key IN (SELECT dedupe_key FROM jobs WHERE status = 'queued')"""
    )
    await db.execute("UPDATE jobs SET status = 'queued', started_at = NULL WHERE status = 'running'")


async def prune(db, keep_days: int = 7) -> None:
    await db.execute(
        """DELETE FROM jobs WHERE status IN ('done', 'cancelled')
           AND finished_at < now() - make_interval(days => $1)""",
        keep_days,
    )


class JobContext:
    """Handed to job handlers for throttled progress reporting and cancellation checks."""

    def __init__(self, pool: asyncpg.Pool, job_id: int, interval: float = 1.0):
        self.pool = pool
        self.job_id = job_id
        self.interval = interval
        self._last_progress = 0.0
        self._last_cancel_check = 0.0
        self._cancelled = False

    async def progress(self, fraction: float, message: str | None = None) -> None:
        now = time.monotonic()
        if now - self._last_progress < self.interval:
            return
        self._last_progress = now
        await self.pool.execute(
            "UPDATE jobs SET progress = $2, message = coalesce($3, message) WHERE id = $1",
            self.job_id, fraction, message,
        )

    async def cancelled(self) -> bool:
        now = time.monotonic()
        if not self._cancelled and now - self._last_cancel_check >= self.interval:
            self._last_cancel_check = now
            self._cancelled = await self.pool.fetchval(
                "SELECT cancel_requested FROM jobs WHERE id = $1", self.job_id
            )
        return self._cancelled
