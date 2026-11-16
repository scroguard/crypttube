"""Encryption of users' uploads on a schedule, and what is still left unencrypted.

Uploads through the app are encrypted as they stream in. Files can also reach a user's
library without the app (copied into the uploads volume), or have been uploaded before
encryption was turned on. The `encrypt_uploads` job (app.worker) encrypts those in place;
the worker queues it every `encrypt_uploads_interval_minutes` while a key is set. It covers
every media file in users' libraries on disk, indexed or not, and every performer/studio/tag
picture (generated/entities).

It is safe to run on a schedule:
- files changed within `encrypt_settle_seconds` are left for the next run (still being written);
- a file is encrypted once: an encrypted one is recognised by its header and skipped;
- a file replaced or written to while being encrypted is left alone (crypto.encrypt_file_in_place);
- one run at a time: a single dedupe key, and a queued or running job blocks a new one;
- a run with nothing to do walks the folders and reads each file's first bytes; while it
  runs, scans and previews wait (see below), so a very large library may want a longer interval;
- it never runs beside a scan or preview generation: like scans it is an exclusive job
  (app.worker.claim_next), so nothing indexes a file while it is being swapped.
"""

import os

import asyncpg

from app import crypto, jobs, media

JOB_KIND = "encrypt_uploads"
DEDUPE_KEY = "encrypt_uploads"
LEFT_KEY = "encrypt_uploads_left"     # system_state: files the last full run left unencrypted


async def library_roots(db) -> list[str]:
    return [r["path"] for r in await db.fetch("SELECT path FROM library_paths WHERE owner_id IS NOT NULL ORDER BY id")]


def media_files(roots: list[str]) -> list[str]:
    """Media files in these folders, walked like the scanner does: hidden files and folders
    (in-progress uploads and encryptions) and symlinks are skipped."""
    out = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")
                                 and not os.path.islink(os.path.join(dirpath, d)))
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                if name.startswith(".") or os.path.islink(path) or media.classify(name) is None:
                    continue
                out.append(path)
    return out


async def enqueue_if_due(pool: asyncpg.Pool, interval_minutes: float) -> int | None:
    """Queue a scheduled run if encryption is on and none was queued, ran or is running
    within the interval."""
    if not crypto.enabled() or interval_minutes <= 0:
        return None
    recent = await pool.fetchval(
        """SELECT 1 FROM jobs WHERE kind = $1
           AND (status IN ('queued', 'running') OR created_at > now() - make_interval(secs => $2))
           LIMIT 1""", JOB_KIND, float(interval_minutes) * 60)
    if recent:
        return None
    return await jobs.enqueue(pool, JOB_KIND, {"scheduled": True}, dedupe_key=DEDUPE_KEY)
