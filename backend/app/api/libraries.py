import asyncio
import os
import re
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


BROWSE_LIMIT = 5000


def _natural(name: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _list_subdirs(path: Path, skip: Path) -> list[str]:
    """Visible subdirectories, mirroring what the scanner walks: hidden directories and
    symlinked directories are skipped (os.walk doesn't follow links)."""
    names = []
    with os.scandir(path) as it:
        for entry in it:
            if entry.name.startswith("."):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if Path(entry.path) == skip:
                continue
            names.append(entry.name)
    return sorted(names, key=_natural)


@router.get("/browse")
async def browse(request: Request, path: str | None = None) -> dict:
    """List the subdirectories of a folder under the media root, for picking a library
    path or a subfolder to scan. Never lists anything outside the media root."""
    settings = get_settings()
    media = settings.media_dir.resolve()
    if path is not None and not path.startswith("/"):
        raise HTTPException(422, "path must be absolute")
    target = Path(path).resolve() if path else media
    if not target.is_relative_to(media):
        raise HTTPException(422, f"path must be inside {media}")
    if not target.is_dir():
        raise HTTPException(404, "folder not found")
    try:
        names = await asyncio.to_thread(_list_subdirs, target, settings.generated_dir.resolve())
    except PermissionError:
        raise HTTPException(403, "folder is not readable by the server") from None

    libraries = {r["path"]: r["id"] for r in await request.app.state.pool.fetch(
        "SELECT id, path FROM library_paths")}
    containing = next(({"id": lid, "path": lp} for lp, lid in libraries.items()
                       if target == Path(lp) or target.is_relative_to(lp)), None)
    entries = [{"name": n, "path": str(target / n), "library_id": libraries.get(str(target / n))}
               for n in names[:BROWSE_LIMIT]]
    return {
        "path": str(target),
        "root": str(media),
        "parent": str(target.parent) if target != media else None,
        "library": containing,
        "entries": entries,
        "truncated": len(names) > BROWSE_LIMIT,
    }


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
        if not sub_path.is_dir():
            # A manual scan of a mistyped path would mark everything "under" it missing.
            raise HTTPException(422, "folder does not exist")
        sub = None if str(sub_path) == lib_path else str(sub_path)
    job_id = await jobs.enqueue(pool, "scan", {"library_id": library_id, "path": sub},
                                dedupe_key=scan_dedupe_key(library_id, sub))
    return {"job_id": job_id}


@router.post("/scan", status_code=status.HTTP_202_ACCEPTED)
async def scan_all(request: Request) -> dict:
    job_id = await jobs.enqueue(request.app.state.pool, "scan", {"library_id": None},
                                dedupe_key=scan_dedupe_key(None))
    return {"job_id": job_id}
