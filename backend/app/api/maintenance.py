"""Admin: cleanup of removed media and routine maintenance (see app.maintenance)."""

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app import jobs, maintenance

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
