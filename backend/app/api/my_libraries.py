"""A user's own libraries: create, share, upload into, and delete.

Files live in the `uploads` volume at <uploads_dir>/<user id>/<random folder>/...; the
folder is registered as a library owned by the user, so uploads go through the same
scan / probe / generate pipeline as admin libraries, and visibility is enforced by
app.acl like everything else. Only the owner can change a library or its sharing.
"""

import asyncio
import os
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import ClientDisconnect

from app import counts, crypto, jobs, media
from app.auth import User, require_user
from app.config import get_settings
from app.generated import FILE_MODE, ensure_dir
from app.watcher import scan_dedupe_key

router = APIRouter(prefix="/my", tags=["my libraries"])

MAX_DEPTH = 8
MAX_NAME = 200
WRITE_BUFFER = 1024 * 1024
Visibility = Literal["private", "users", "everyone"]


class LibraryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    visibility: Visibility = "private"
    shared_user_ids: list[int] = []


class LibraryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=100)
    visibility: Visibility | None = None
    shared_user_ids: list[int] | None = None


def _require_upload(user: User) -> None:
    if not user.can_upload:
        raise HTTPException(403, "uploads are not enabled for your account")


def user_root(user_id: int) -> Path:
    return get_settings().uploads_dir / str(user_id)


def _disk_usage(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _safe_relative(raw: str) -> PurePosixPath:
    """Validate a client-supplied path inside a library: relative, no traversal, no
    hidden or odd names, a media file at the end."""
    path = PurePosixPath(raw.replace("\\", "/"))
    parts = path.parts
    if not parts or path.is_absolute() or len(parts) > MAX_DEPTH:
        raise HTTPException(422, "invalid path")
    for part in parts:
        if (part in (".", "..") or part.startswith(".") or len(part) > MAX_NAME
                or re.search(r"[\x00-\x1f\x7f]", part) or part != part.strip()):
            raise HTTPException(422, f"invalid name: {part!r}")
    if media.classify(parts[-1]) is None:
        raise HTTPException(415, "unsupported file type (videos, images and zip archives only)")
    return path


async def _own_library(request: Request, user: User, library_id: int):
    row = await request.app.state.pool.fetchrow(
        "SELECT id, path, name, visibility FROM library_paths WHERE id = $1 AND owner_id = $2",
        library_id, user.id)
    if row is None:
        raise HTTPException(404, "library not found")
    return row


async def _set_shares(conn, library_id: int, owner_id: int, user_ids: list[int]) -> None:
    wanted = sorted(set(user_ids) - {owner_id})
    found = await conn.fetchval(
        "SELECT count(*) FROM users WHERE id = ANY($1::bigint[]) AND is_active", wanted)
    if found != len(wanted):
        raise HTTPException(422, "unknown user in shared_user_ids")
    await conn.execute("DELETE FROM library_shares WHERE library_id = $1", library_id)
    await conn.execute(
        "INSERT INTO library_shares (library_id, user_id) SELECT $1, unnest($2::bigint[])",
        library_id, wanted)


LIBRARY_SELECT = """
    SELECT l.id, l.name, l.visibility, l.created_at,
           (SELECT coalesce(jsonb_agg(jsonb_build_object('id', u.id, 'username', u.username)
                                      ORDER BY lower(u.username)), '[]')
            FROM library_shares sh JOIN users u ON u.id = sh.user_id WHERE sh.library_id = l.id) AS shared_with,
           (SELECT count(*) FROM files f WHERE f.library_id = l.id AND f.zip_file_id IS NULL) AS file_count,
           (SELECT coalesce(sum(f.size), 0) FROM files f
            WHERE f.library_id = l.id AND f.zip_file_id IS NULL)::bigint AS bytes,
           (SELECT count(*) FROM scenes s WHERE s.library_id = l.id) AS scene_count,
           (SELECT count(*) FROM images i WHERE i.library_id = l.id) AS image_count
    FROM library_paths l"""


@router.get("/libraries")
async def my_libraries(request: Request, user: User = Depends(require_user)) -> dict:
    rows = await request.app.state.pool.fetch(
        f"{LIBRARY_SELECT} WHERE l.owner_id = $1 ORDER BY lower(l.name), l.id", user.id)
    used = await asyncio.to_thread(_disk_usage, user_root(user.id)) if user.can_upload else 0
    return {"can_upload": user.can_upload, "quota_bytes": user.upload_quota_bytes,
            "used_bytes": used, "libraries": [dict(r) for r in rows]}


@router.get("/share-targets")
async def share_targets(request: Request, user: User = Depends(require_user)) -> list[dict]:
    """Users a library can be shared with (only for accounts that can upload)."""
    _require_upload(user)
    rows = await request.app.state.pool.fetch(
        "SELECT id, username FROM users WHERE is_active AND id <> $1 ORDER BY lower(username)", user.id)
    return [dict(r) for r in rows]


@router.post("/libraries", status_code=201)
async def create_library(body: LibraryBody, request: Request, user: User = Depends(require_user)) -> dict:
    _require_upload(user)
    pool = request.app.state.pool
    path = user_root(user.id) / uuid.uuid4().hex
    await asyncio.to_thread(ensure_dir, path)
    async with pool.acquire() as conn, conn.transaction():
        library_id = await conn.fetchval(
            """INSERT INTO library_paths (path, owner_id, name, visibility, watch)
               VALUES ($1, $2, $3, $4, false) RETURNING id""",
            str(path), user.id, body.name.strip(), body.visibility)
        await _set_shares(conn, library_id, user.id, body.shared_user_ids)
    await counts.request_refresh(pool)
    return dict(await pool.fetchrow(f"{LIBRARY_SELECT} WHERE l.id = $1", library_id))


@router.patch("/libraries/{library_id}")
async def update_library(library_id: int, body: LibraryPatch, request: Request,
                         user: User = Depends(require_user)) -> dict:
    await _own_library(request, user, library_id)
    pool = request.app.state.pool
    data = body.model_dump(exclude_unset=True)
    if any(v is None for v in data.values()):
        raise HTTPException(422, "fields cannot be null")
    async with pool.acquire() as conn, conn.transaction():
        if "name" in data:
            await conn.execute("UPDATE library_paths SET name = $2 WHERE id = $1", library_id, data["name"].strip())
        if "visibility" in data:
            await conn.execute("UPDATE library_paths SET visibility = $2 WHERE id = $1", library_id, data["visibility"])
        if "shared_user_ids" in data:
            await _set_shares(conn, library_id, user.id, data["shared_user_ids"])
    await counts.request_refresh(pool)  # other users' counts change with what they can see
    return dict(await pool.fetchrow(f"{LIBRARY_SELECT} WHERE l.id = $1", library_id))


async def remove_library(pool, library_id: int, path: str) -> None:
    """Delete a user library: index rows (cascading to scenes/images/galleries) and files."""
    await pool.execute("DELETE FROM library_paths WHERE id = $1", library_id)
    uploads = get_settings().uploads_dir.resolve()
    target = Path(path).resolve()
    if target != uploads and target.is_relative_to(uploads):  # never delete outside the uploads volume
        await asyncio.to_thread(shutil.rmtree, target, True)
    await counts.request_refresh(pool)


@router.delete("/libraries/{library_id}", status_code=204)
async def delete_library(library_id: int, request: Request, user: User = Depends(require_user)) -> Response:
    lib = await _own_library(request, user, library_id)
    await remove_library(request.app.state.pool, library_id, lib["path"])
    return Response(status_code=204)


@router.post("/libraries/{library_id}/regenerate", status_code=202)
async def regenerate_library(library_id: int, request: Request, user: User = Depends(require_user)) -> dict:
    """Rebuild all previews and thumbnails of one of my libraries."""
    await _own_library(request, user, library_id)
    job_id = await jobs.enqueue(request.app.state.pool, "generate", {"force": True, "library_id": library_id},
                                dedupe_key=f"generate:force:{library_id}")
    return {"job_id": job_id}


FILE_LIST_LIMIT = 5000


def _walk_files(root: Path) -> list[tuple[str, int]]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue  # in-progress uploads
            full = os.path.join(dirpath, name)
            try:
                out.append((full, os.lstat(full).st_size))
            except OSError:
                pass
            if len(out) >= FILE_LIST_LIMIT:
                return out
    return out


def _natural(text: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


@router.get("/libraries/{library_id}/files")
async def list_files(library_id: int, request: Request, user: User = Depends(require_user)) -> list[dict]:
    """What is actually on disk in this library, with the indexed scene/image for each
    file. Files that couldn't be indexed (e.g. corrupt) are listed too, so they can be
    seen and deleted rather than silently using up quota."""
    lib = await _own_library(request, user, library_id)
    on_disk = await asyncio.to_thread(_walk_files, Path(lib["path"]))
    indexed = {r["path"]: r for r in await request.app.state.pool.fetch(
        """SELECT f.path, s.id AS scene_id, i.id AS image_id
           FROM files f LEFT JOIN scenes s ON s.file_id = f.id LEFT JOIN images i ON i.file_id = f.id
           WHERE f.library_id = $1 AND f.zip_file_id IS NULL""", library_id)}
    prefix = len(lib["path"]) + 1
    files = [{"path": full[prefix:], "size": size, "indexed": full in indexed,
              "scene_id": indexed[full]["scene_id"] if full in indexed else None,
              "image_id": indexed[full]["image_id"] if full in indexed else None}
             for full, size in on_disk]
    return sorted(files, key=lambda f: _natural(f["path"]))


@router.put("/libraries/{library_id}/files", status_code=201)
async def upload_file(library_id: int, request: Request, path: str = Query(max_length=1000),
                      overwrite: bool = False, user: User = Depends(require_user)) -> dict:
    """Upload one file: the raw bytes are the request body, streamed straight to disk.
    `path` is the destination inside the library (folders are created as needed)."""
    _require_upload(user)
    lib = await _own_library(request, user, library_id)
    rel = _safe_relative(path)
    dest = Path(lib["path"]) / rel
    if dest.exists() and not overwrite:
        raise HTTPException(409, "a file with that name already exists")

    quota = user.upload_quota_bytes
    used = await asyncio.to_thread(_disk_usage, user_root(user.id)) if quota is not None else 0
    declared = int(request.headers.get("content-length") or 0)
    if quota is not None and used + declared > quota:
        raise HTTPException(413, "upload would exceed your storage quota")

    await asyncio.to_thread(ensure_dir, dest.parent)
    tmp = dest.parent / f".upload-{uuid.uuid4().hex}"  # hidden: the scanner ignores it
    received = 0
    try:
        with open(tmp, "wb") as f:
            # With encryption on, the upload is encrypted as it streams in: the plain
            # content never reaches the disk.
            out = crypto.EncryptingWriter(f) if crypto.enabled() else f
            buffer = bytearray()
            async for chunk in request.stream():
                received += len(chunk)
                if quota is not None and used + received > quota:
                    raise HTTPException(413, "upload would exceed your storage quota")
                buffer += chunk
                if len(buffer) >= WRITE_BUFFER:
                    await asyncio.to_thread(out.write, bytes(buffer))
                    buffer.clear()
            if buffer:
                await asyncio.to_thread(out.write, bytes(buffer))
            if out is not f:
                await asyncio.to_thread(out.close)
        if received == 0:
            raise HTTPException(422, "empty upload")
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, dest)
    except ClientDisconnect:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "upload interrupted") from None
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    # Index it now. The file is complete, so the scan needn't wait for it to "settle".
    folder = str(dest.parent) if dest.parent != Path(lib["path"]) else None
    await jobs.enqueue(request.app.state.pool, "scan", {"library_id": library_id, "path": folder, "settle": 0},
                       dedupe_key=scan_dedupe_key(library_id, folder))
    return {"path": str(rel), "size": received}


@router.delete("/libraries/{library_id}/files", status_code=204)
async def delete_file(library_id: int, request: Request, path: str = Query(max_length=1000),
                      user: User = Depends(require_user)) -> Response:
    lib = await _own_library(request, user, library_id)
    rel = _safe_relative(path)
    root = Path(lib["path"])
    target = root / rel
    pool = request.app.state.pool
    existed = target.is_file()
    if existed:
        target.unlink()
        # tidy now-empty folders up to (not including) the library root
        parent = target.parent
        while parent != root and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    deleted = await pool.fetchval(
        "DELETE FROM files WHERE library_id = $1 AND path = $2 AND zip_file_id IS NULL RETURNING id",
        library_id, str(target))
    if not existed and deleted is None:
        raise HTTPException(404, "file not found")
    await pool.execute(
        "DELETE FROM galleries WHERE library_id = $1 AND folder_path IS NOT NULL AND image_count = 0", library_id)
    await pool.execute(
        """UPDATE galleries g SET cover_image_id = (
               SELECT image_id FROM gallery_images WHERE gallery_id = g.id ORDER BY position, image_id LIMIT 1)
           WHERE g.library_id = $1 AND g.cover_image_id IS NULL AND g.image_count > 0""", library_id)
    await counts.request_refresh(pool)
    return Response(status_code=204)
