from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app import jobs, maintenance
from app.config import get_settings

router = APIRouter(tags=["jobs"])


def _redact(job: dict) -> dict:
    """Admins manage the queue but don't get to see inside users' libraries: folder
    names under the uploads volume are replaced, and so are messages that mention one."""
    uploads = str(get_settings().uploads_dir)
    path = job["payload"].get("path") if isinstance(job["payload"], dict) else None
    if isinstance(path, str) and (path == uploads or path.startswith(uploads + "/")):
        job["payload"] = {**job["payload"], "path": "(user library)"}
    for field in ("message", "error"):
        if isinstance(job.get(field), str) and uploads in job[field]:
            job[field] = "(user library)"
    return job

COLUMNS = """id, kind, payload, status, progress, message, error, attempts,
             created_at, started_at, finished_at"""


@router.get("/jobs")
async def list_jobs(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None, description="keyset cursor: id of the last job seen"),
    status: Literal["queued", "running", "done", "failed", "cancelled"] | None = None,
    hide_batches: bool = Query(False, description="leave out preview batches and count refreshes"),
) -> dict:
    # Batches: generate jobs for given scenes/images (made by splitting up a big job, or
    # queued by a scan); the generate jobs an admin starts cover a library or everything.
    rows = await request.app.state.pool.fetch(
        f"""SELECT {COLUMNS} FROM jobs
            WHERE ($1::bigint IS NULL OR id < $1) AND ($2::job_status IS NULL OR status = $2::job_status)
              AND NOT ($4 AND (kind = 'refresh_counts'
                               OR (kind = 'generate' AND (payload ? 'scene_ids' OR payload ? 'image_ids'))))
            ORDER BY id DESC LIMIT $3""",
        before_id, status, limit + 1, hide_batches,
    )
    items = [_redact(dict(r)) for r in rows[:limit]]
    return {"items": items, "next": items[-1]["id"] if len(rows) > limit else None}


class WorkerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generate_jobs: int = Field(ge=1, le=16)


@router.get("/jobs/settings")
async def worker_settings(request: Request) -> dict:
    from app.worker import generate_jobs
    return {"generate_jobs": await generate_jobs(request.app.state.pool, get_settings()), "max_generate_jobs": 16}


@router.put("/jobs/settings")
async def save_worker_settings(body: WorkerSettings, request: Request) -> dict:
    pool = request.app.state.pool
    await maintenance.set_state(pool, "worker_settings", body.model_dump())
    await pool.execute(f"NOTIFY {jobs.NOTIFY_CHANNEL}")   # let the worker start more right away
    return await worker_settings(request)


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, request: Request) -> dict:
    row = await request.app.state.pool.fetchrow(f"SELECT {COLUMNS} FROM jobs WHERE id = $1", job_id)
    if row is None:
        raise HTTPException(404, "job not found")
    return _redact(dict(row))


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, request: Request) -> dict:
    if not await jobs.cancel(request.app.state.pool, job_id):
        raise HTTPException(409, "job is not queued or running")
    return {"ok": True}


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False              # discard and rebuild, instead of only filling gaps
    library_id: int | None = None    # limit to one library


@router.post("/generate", status_code=202)
async def generate(request: Request, body: GenerateRequest | None = None) -> dict:
    """Queue screenshot/sprite generation. By default only what is missing or unusable
    is (re)created; `force` rebuilds everything in scope, including cached thumbnails."""
    body = body or GenerateRequest()
    pool = request.app.state.pool
    if body.library_id is not None and not await pool.fetchval(
            "SELECT true FROM library_paths WHERE id = $1", body.library_id):
        raise HTTPException(404, "library not found")
    payload = {"force": True} if body.force else {"missing": True}
    if body.library_id is not None:
        payload["library_id"] = body.library_id
    key = f"generate:{'force' if body.force else 'missing'}:{body.library_id or 'all'}"
    return {"job_id": await jobs.enqueue(pool, "generate", payload, dedupe_key=key)}
