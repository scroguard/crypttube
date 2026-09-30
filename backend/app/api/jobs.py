from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app import jobs

router = APIRouter(tags=["jobs"])

COLUMNS = """id, kind, payload, status, progress, message, error, attempts,
             created_at, started_at, finished_at"""


@router.get("/jobs")
async def list_jobs(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None, description="keyset cursor: id of the last job seen"),
    status: Literal["queued", "running", "done", "failed", "cancelled"] | None = None,
) -> dict:
    rows = await request.app.state.pool.fetch(
        f"""SELECT {COLUMNS} FROM jobs
            WHERE ($1::bigint IS NULL OR id < $1) AND ($2::job_status IS NULL OR status = $2::job_status)
            ORDER BY id DESC LIMIT $3""",
        before_id, status, limit + 1,
    )
    items = [dict(r) for r in rows[:limit]]
    return {"items": items, "next": items[-1]["id"] if len(rows) > limit else None}


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, request: Request) -> dict:
    row = await request.app.state.pool.fetchrow(f"SELECT {COLUMNS} FROM jobs WHERE id = $1", job_id)
    if row is None:
        raise HTTPException(404, "job not found")
    return dict(row)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, request: Request) -> dict:
    if not await jobs.cancel(request.app.state.pool, job_id):
        raise HTTPException(409, "job is not queued or running")
    return {"ok": True}


@router.post("/generate", status_code=202)
async def generate_missing(request: Request) -> dict:
    """Queue screenshot/sprite generation for every scene that lacks them."""
    job_id = await jobs.enqueue(request.app.state.pool, "generate", {"missing": True},
                                dedupe_key="generate:missing")
    return {"job_id": job_id}
