"""Admin: cleanup of removed media and routine maintenance (see app.maintenance)."""

import asyncio
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app import counts, crypto, jobs, maintenance

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Settings(Body):
    enabled: bool
    hour: int = Field(ge=0, le=23)
    remove_missing_after_days: int = Field(ge=0, le=3650)


class CleanupRequest(Body):
    dry_run: bool = True
    scan: bool = True                 # rescan first, so files deleted since the last scan are noticed
    min_missing_days: int = Field(0, ge=0, le=3650)


@router.get("")
async def overview(request: Request) -> dict:
    pool = request.app.state.pool
    active = await pool.fetch(
        """SELECT DISTINCT ON (kind) id, kind, status, progress, message, error, finished_at FROM jobs
           WHERE kind = ANY($1::text[]) ORDER BY kind, id DESC""",
        [maintenance.JOB_KIND, maintenance.CLEANUP_KIND])
    return {
        "settings": await maintenance.get_settings(pool),
        "server_time": datetime.now().astimezone().isoformat(timespec="minutes"),
        "database_bytes": await pool.fetchval("SELECT pg_database_size(current_database())"),
        "missing_files": await pool.fetchval("SELECT count(*) FROM files WHERE missing_since IS NOT NULL"),
        "maintenance_report": await maintenance.get_state(pool, maintenance.MAINTENANCE_REPORT),
        "cleanup_report": await maintenance.get_state(pool, maintenance.CLEANUP_REPORT),
        "jobs": {r["kind"]: dict(r) for r in active},
    }


def _encryption_counts(paths: list[str]) -> tuple[int, int]:
    present = [p for p in paths if os.path.exists(p)]
    return len(present), sum(crypto.is_encrypted(p) for p in present)


async def encryption_status(pool) -> dict:
    paths = [r["path"] for r in await pool.fetch(
        """SELECT f.path FROM files f JOIN library_paths l ON l.id = f.library_id
           WHERE l.owner_id IS NOT NULL AND f.zip_file_id IS NULL AND f.missing_since IS NULL""")]
    total, encrypted = await asyncio.to_thread(_encryption_counts, paths)
    job = await pool.fetchrow(
        "SELECT id, status, progress, message, error FROM jobs WHERE kind = 'encrypt_uploads' ORDER BY id DESC LIMIT 1")
    return {"enabled": crypto.enabled(), "key_id": crypto.key_id().hex() if crypto.enabled() else None,
            "uploaded_files": total, "encrypted_files": encrypted, "job": dict(job) if job else None}


@router.get("/encryption")
async def encryption(request: Request) -> dict:
    """Whether users' uploads are encrypted at rest, and how far along existing files are."""
    return await encryption_status(request.app.state.pool)


@router.post("/encryption/run", status_code=202)
async def encrypt_now(request: Request) -> dict:
    if not crypto.enabled():
        raise HTTPException(409, "encryption is off: no uploads key is configured")
    return {"job_id": await jobs.enqueue(request.app.state.pool, "encrypt_uploads", {}, dedupe_key="encrypt_uploads")}


@router.put("/settings")
async def save_settings(body: Settings, request: Request) -> dict:
    await maintenance.set_state(request.app.state.pool, maintenance.SETTINGS_KEY, body.model_dump())
    return body.model_dump()


@router.post("/run", status_code=202)
async def run_now(request: Request) -> dict:
    return {"job_id": await jobs.enqueue(request.app.state.pool, maintenance.JOB_KIND, {},
                                         dedupe_key=maintenance.JOB_KIND)}


@router.post("/cleanup", status_code=202)
async def cleanup(request: Request, body: CleanupRequest | None = None) -> dict:
    """Queue a cleanup. A dry run (the default) only reports what would be removed."""
    body = body or CleanupRequest()
    return {"job_id": await jobs.enqueue(request.app.state.pool, maintenance.CLEANUP_KIND, body.model_dump(),
                                         dedupe_key=maintenance.CLEANUP_KIND)}


# ---------------------------------------------------------------------------
# Files the scanner couldn't read (main libraries only: users' own stay private)
# ---------------------------------------------------------------------------

EXPLANATIONS = (
    ("moov atom not found", "Incomplete video: its index is missing. Usually an interrupted download or copy "
                            "(or a file that isn't really an MP4/MOV)."),
    ("invalid data found when processing input", "Not readable as a video: damaged, empty, or not really a video."),
    ("no video stream", "The file has no video in it (audio only, or a broken video track)."),
    ("timed out", "Reading it took too long (a very damaged file, or very slow storage)."),
    ("unreadable image", "Not readable as a picture: damaged, incomplete, or not really an image."),
    ("bad archive", "Not a readable zip archive (damaged or incomplete)."),
    ("permission denied", "The server isn't allowed to read this file (check its permissions)."),
    ("end of file", "The file ends early (incomplete)."),
)


def explain(reason: str) -> str:
    low = reason.lower()
    return next((text for needle, text in EXPLANATIONS if needle in low), "The file couldn't be read as media.")


class UnreadableAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(min_length=1, max_length=5000)
    ignored: bool = True


MAIN_ONLY = "JOIN library_paths l ON l.id = u.library_id AND l.owner_id IS NULL"


@router.get("/unreadable")
async def unreadable(request: Request, show_ignored: bool = False, limit: int = 200, offset: int = 0) -> dict:
    pool = request.app.state.pool
    limit = max(1, min(limit, 1000))
    rows = await pool.fetch(
        f"""SELECT u.path, u.size, u.reason, u.ignored, u.first_seen, u.last_seen FROM unreadable_files u {MAIN_ONLY}
            WHERE $1 OR NOT u.ignored ORDER BY u.path COLLATE natural_sort LIMIT $2 OFFSET $3""",
        show_ignored, limit, max(0, offset))
    counts = await pool.fetchrow(
        f"""SELECT count(*) FILTER (WHERE NOT u.ignored) AS shown, count(*) FILTER (WHERE u.ignored) AS ignored
            FROM unreadable_files u {MAIN_ONLY}""")
    return {"items": [dict(r) | {"explanation": explain(r["reason"])} for r in rows],
            "total": counts["shown"], "ignored": counts["ignored"]}


async def _rows(db, paths: list[str]):
    return await db.fetch(
        f"""SELECT u.path, u.library_id, u.size, u.mod_time, l.path AS library_path
            FROM unreadable_files u {MAIN_ONLY} WHERE u.path = ANY($1::text[])""", paths)


@router.post("/unreadable/ignore")
async def ignore_unreadable(body: UnreadableAction, request: Request) -> dict:
    """Hide (or show again) files in the list. They stay skipped by scans either way."""
    pool = request.app.state.pool
    paths = [r["path"] for r in await _rows(pool, body.paths)]
    await pool.execute("UPDATE unreadable_files SET ignored = $2 WHERE path = ANY($1::text[])", paths, body.ignored)
    return {"updated": len(paths)}


@router.post("/unreadable/retry", status_code=202)
async def retry_unreadable(body: UnreadableAction, request: Request) -> dict:
    """Forget that these couldn't be read and scan their folders again (e.g. after fixing them)."""
    from app.watcher import scan_dedupe_key
    # Forget them and queue the scans together: if queueing fails, nothing is forgotten.
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        rows = await _rows(conn, body.paths)
        await conn.execute("DELETE FROM unreadable_files WHERE path = ANY($1::text[])", [r["path"] for r in rows])
        folders = {(r["library_id"], os.path.dirname(r["path"])) for r in rows}
        for library_id, folder in sorted(folders):
            await jobs.enqueue(conn, "scan", {"library_id": library_id, "path": folder, "settle": 0},
                               dedupe_key=scan_dedupe_key(library_id, folder))
    return {"retrying": len(rows), "scans": len(folders)}


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _unlink_if_unchanged(path: str, size: int, mod_time: datetime) -> bool:
    """Delete the file only if it is still the one found unreadable (same size and time,
    as the scanner records them). False if it has changed since, e.g. been repaired."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return True
    if st.st_size != size or EPOCH + timedelta(microseconds=st.st_mtime_ns // 1000) != mod_time:
        return False
    os.unlink(path)
    return True


@router.post("/unreadable/delete")
async def delete_unreadable(body: UnreadableAction, request: Request) -> dict:
    """Delete unreadable files from disk, with whatever entry they have (a file that was
    indexed fine and later damaged still has its old scene or image)."""
    from app.watcher import scan_dedupe_key
    pool = request.app.state.pool
    deleted, problems, changed = [], [], set()
    for r in await _rows(pool, body.paths):
        if not r["path"].startswith(r["library_path"].rstrip("/") + "/"):
            problems.append(f"{r['path']}: outside its library")
            continue
        try:
            if not await asyncio.to_thread(_unlink_if_unchanged, r["path"], r["size"], r["mod_time"]):
                problems.append(f"{r['path']}: it has changed since it was found unreadable, so it was kept "
                                "and is being checked again")
                changed.add((r["library_id"], os.path.dirname(r["path"])))
                continue
        except OSError as e:
            problems.append(f"{r['path']}: {e.strerror or e}")
            continue
        deleted.append(r["path"])
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM unreadable_files WHERE path = ANY($1::text[])", deleted)
        # its entry (if it had one) goes too; previews left behind are swept by maintenance
        gone = await conn.execute("DELETE FROM files WHERE zip_file_id IS NULL AND path = ANY($1::text[])", deleted)
        for library_id, folder in sorted(changed):
            await jobs.enqueue(conn, "scan", {"library_id": library_id, "path": folder, "settle": 0},
                               dedupe_key=scan_dedupe_key(library_id, folder))
    if gone != "DELETE 0":
        await counts.request_refresh(pool)
    if problems and not deleted:
        raise HTTPException(409, "nothing was deleted: " + problems[0] + (
            " (the media store must be writable: MEDIA_MODE=rw, and PUID allowed to delete there)"
            if "read-only" in problems[0].lower() or "permission" in problems[0].lower() else ""))
    return {"deleted": len(deleted), "problems": problems}
