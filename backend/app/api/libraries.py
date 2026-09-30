# TODO(phase 5): restrict to admins once auth lands.
from pathlib import Path

import asyncpg
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app import jobs
from app.config import get_settings
from app.watcher import scan_dedupe_key
from app.worker import LIBRARIES_CHANNEL

router = APIRouter(prefix="/libraries", tags=["libraries"])

COLUMNS = "id, path, enabled, watch, create_folder_galleries, exclude_patterns, created_at"


class LibraryIn(BaseModel):
    path: str
    enabled: bool = True
    watch: bool = False
    create_folder_galleries: bool = True
    exclude_patterns: list[str] = Field(default_factory=list)


class LibraryPatch(BaseModel):
    enabled: bool | None = None
    watch: bool | None = None
    create_folder_galleries: bool | None = None
    exclude_patterns: list[str] | None = None


class ScanRequest(BaseModel):
    path: str | None = None  # optional subdirectory of the library


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


async def _validate_path(pool: asyncpg.Pool, raw: str) -> Path:
    settings = get_settings()
    if not raw.startswith("/"):
        raise HTTPException(422, "path must be absolute")
    path = Path(raw).resolve()
    media_dir = settings.media_dir.resolve()
    if not path.is_relative_to(media_dir):
        raise HTTPException(422, f"path must be inside {media_dir}")
    if _overlaps(path, settings.generated_dir.resolve()):
        raise HTTPException(422, "path must not overlap the generated-content directory")
    if not path.is_dir():
        raise HTTPException(422, "path does not exist or is not a directory")
    for r in await pool.fetch("SELECT path FROM library_paths"):
        if _overlaps(path, Path(r["path"])):
            raise HTTPException(409, f"path overlaps existing library {r['path']}")
    return path


def _row(r) -> dict:
    return dict(r)


@router.get("")
async def list_libraries(request: Request) -> list[dict]:
    rows = await request.app.state.pool.fetch(f"SELECT {COLUMNS} FROM library_paths ORDER BY path")
    return [_row(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_library(body: LibraryIn, request: Request) -> dict:
    pool = request.app.state.pool
    path = await _validate_path(pool, body.path)
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            f"""INSERT INTO library_paths (path, enabled, watch, create_folder_galleries, exclude_patterns)
                VALUES ($1, $2, $3, $4, $5) RETURNING {COLUMNS}""",
            str(path), body.enabled, body.watch, body.create_folder_galleries, body.exclude_patterns,
        )
        await conn.execute(f"NOTIFY {LIBRARIES_CHANNEL}")
    return _row(row)


@router.patch("/{library_id}")
async def update_library(library_id: int, body: LibraryPatch, request: Request) -> dict:
    pool = request.app.state.pool
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(422, "nothing to update")
    sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(fields, start=2))
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            f"UPDATE library_paths SET {sets} WHERE id = $1 RETURNING {COLUMNS}",
            library_id, *fields.values(),
        )
        if row is None:
            raise HTTPException(404, "library not found")
        await conn.execute(f"NOTIFY {LIBRARIES_CHANNEL}")
    return _row(row)


@router.delete("/{library_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_library(library_id: int, request: Request) -> None:
    """Removes the library and everything indexed from it. Media files are untouched."""
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        path = await conn.fetchval("DELETE FROM library_paths WHERE id = $1 RETURNING path", library_id)
        if path is None:
            raise HTTPException(404, "library not found")
        # Folder galleries aren't FK-linked to files; drop the ones that lived here.
        await conn.execute(
            "DELETE FROM galleries WHERE folder_path = $1 OR starts_with(folder_path, $2)",
            path, path.rstrip("/") + "/",
        )
        await conn.execute(f"NOTIFY {LIBRARIES_CHANNEL}")


@router.post("/{library_id}/scan", status_code=status.HTTP_202_ACCEPTED)
async def scan_library(library_id: int, request: Request, body: ScanRequest | None = None) -> dict:
    pool = request.app.state.pool
    lib_path = await pool.fetchval("SELECT path FROM library_paths WHERE id = $1", library_id)
    if lib_path is None:
        raise HTTPException(404, "library not found")
    sub = None
    if body and body.path:
        sub_path = Path(body.path).resolve()
        if not sub_path.is_relative_to(lib_path):
            raise HTTPException(422, "path is outside the library")
        sub = None if str(sub_path) == lib_path else str(sub_path)
    job_id = await jobs.enqueue(pool, "scan", {"library_id": library_id, "path": sub},
                                dedupe_key=scan_dedupe_key(library_id, sub))
    return {"job_id": job_id}


@router.post("/scan", status_code=status.HTTP_202_ACCEPTED)
async def scan_all(request: Request) -> dict:
    job_id = await jobs.enqueue(request.app.state.pool, "scan", {"library_id": None},
                                dedupe_key=scan_dedupe_key(None))
    return {"job_id": job_id}
