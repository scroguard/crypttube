"""Link folders to performers and studios, so media arriving there gets tagged
(see app/folder_links.py).

Tagging a library's content needs the same rights as editing it by hand, whatever the
picker offered: an editor or admin for the main libraries, the owner for a user's own.
Being able to see a library someone shared isn't enough. Reading a link needs those rights
too, because a link names a folder.

A folder takes any number of performers but one studio, counting the folders above and
below it, because a file has one studio.
"""

import asyncio
import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app import folder_links, jobs
from app.acl import Acl
from app.api import entities
from app.api.move import BROWSE_LIMIT, _root_for, _subfolders
from app.auth import get_acl
from app.config import get_settings

router = APIRouter(prefix="/folder-links", tags=["folder-links"])

Kind = Literal["performers", "studios"]
COLUMN = {"performers": "performer_id", "studios": "studio_id"}


async def _roots(pool, acl: Acl) -> list[dict]:
    rows = await pool.fetch(
        """SELECT id, path, name, owner_id, enabled, watch FROM library_paths
           WHERE ($1 AND owner_id IS NULL) OR owner_id = $2
           ORDER BY owner_id NULLS FIRST, lower(coalesce(name, path))""", acl.editor, acl.user_id)
    return [{"library_id": r["id"], "path": r["path"], "name": r["name"] or r["path"],
             "mine": r["owner_id"] is not None, "watching": r["enabled"] and r["watch"]} for r in rows]


def _folder(raw: str, roots: list[dict]) -> tuple[Path, dict]:
    if not raw.startswith("/"):
        raise HTTPException(422, "path must be absolute")
    path = Path(raw).resolve()
    root = _root_for(roots, path)
    if root is None:                # checked first: says nothing about folders elsewhere
        raise HTTPException(403, "that folder is not in a library you can tag")
    if not path.is_dir():
        raise HTTPException(404, "folder not found")
    return path, root


async def _require_entity(request: Request, acl: Acl, kind: Kind, entity_id: int) -> None:
    get = entities.get_performer if kind == "performers" else entities.get_studio
    await get(entity_id, request, acl)      # 404 unless visible


@router.get("/folders")
async def folders(request: Request, path: str | None = None, acl: Acl = Depends(get_acl)) -> dict:
    """Browse the folders you can link. Without `path`: the libraries you can use."""
    roots = await _roots(request.app.state.pool, acl)
    if path is None:
        return {"path": None, "parent": None, "library": None, "entries": [
            {"name": r["name"], "path": r["path"], "mine": r["mine"]} for r in roots]}
    folder, root = _folder(path, roots)
    try:
        names = await asyncio.to_thread(_subfolders, folder, get_settings().generated_dir.resolve())
    except PermissionError:
        raise HTTPException(403, "folder is not readable by the server") from None
    at_root = folder == Path(root["path"])
    return {"path": str(folder), "parent": None if at_root else str(folder.parent),
            "library": {"name": root["name"], "path": root["path"], "mine": root["mine"]},
            "relative": "" if at_root else str(folder.relative_to(root["path"])),
            "truncated": len(names) > BROWSE_LIMIT,
            "entries": [{"name": n, "path": str(folder / n)} for n in names[:BROWSE_LIMIT]]}


@router.get("/{kind}/{entity_id}")
async def list_links(kind: Kind, entity_id: int, request: Request, acl: Acl = Depends(get_acl)) -> dict:
    await _require_entity(request, acl, kind, entity_id)
    pool = request.app.state.pool
    roots = {r["library_id"]: r for r in await _roots(pool, acl)}
    rows = await pool.fetch(
        f"""SELECT w.id, w.path, w.library_id FROM folder_links w
            WHERE w.{COLUMN[kind]} = $1 AND w.library_id = ANY($2::bigint[])
            ORDER BY w.path""", entity_id, list(roots))
    missing = await asyncio.to_thread(lambda: {r["id"]: not os.path.isdir(r["path"]) for r in rows})
    return {"can_link": bool(roots), "links": [
        {"id": r["id"], "path": r["path"], "library": roots[r["library_id"]]["name"],
         "relative": os.path.relpath(r["path"], roots[r["library_id"]]["path"]),
         "watching": roots[r["library_id"]]["watching"], "missing": missing[r["id"]]} for r in rows]}


class LinkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4000)
    # "all" also tags the files already there, in the background.
    scope: Literal["new", "all"] = "new"


@router.post("/{kind}/{entity_id}", status_code=201)
async def add_link(kind: Kind, entity_id: int, body: LinkBody, request: Request,
                   acl: Acl = Depends(get_acl)) -> dict:
    """Linking a folder again is harmless: "all" tags what's there again, and the link now
    rests on your rights, which revives one whose creator lost theirs."""
    await _require_entity(request, acl, kind, entity_id)
    pool = request.app.state.pool
    path, root = _folder(body.path, await _roots(pool, acl))
    column = COLUMN[kind]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('folder_links'))")  # one link at a time
        if kind == "studios" and await conn.fetchval(
                """SELECT EXISTS (SELECT 1 FROM folder_links
                                  WHERE studio_id IS NOT NULL AND studio_id <> $1
                                    AND (path = $2 OR starts_with(path, $2 || '/') OR starts_with($2, path || '/')))""",
                entity_id, str(path)):
            raise HTTPException(409, "another studio is already linked to this folder, or to one above or below it")
        link_id = await conn.fetchval(
            f"""INSERT INTO folder_links ({column}, library_id, path, created_by) VALUES ($1, $2, $3, $4)
                ON CONFLICT ({column}, path) WHERE {column} IS NOT NULL DO UPDATE SET created_by = EXCLUDED.created_by
                RETURNING id""", entity_id, root["library_id"], str(path), acl.user_id)
    job_id = None
    if body.scope == "all":
        job_id = await jobs.enqueue(pool, folder_links.JOB_KIND, {"link_id": link_id},
                                    dedupe_key=f"{folder_links.JOB_KIND}:{link_id}")
    return {"id": link_id, "path": str(path), "job_id": job_id}


@router.delete("/{kind}/{entity_id}/{link_id}", status_code=204)
async def remove_link(kind: Kind, entity_id: int, link_id: int, request: Request,
                      acl: Acl = Depends(get_acl)) -> Response:
    """Stops future tagging. Tags already given stay."""
    await _require_entity(request, acl, kind, entity_id)
    pool = request.app.state.pool
    roots = await _roots(pool, acl)
    removed = await pool.fetchval(
        f"""DELETE FROM folder_links WHERE id = $1 AND {COLUMN[kind]} = $2
            AND library_id = ANY($3::bigint[]) RETURNING id""",
        link_id, entity_id, [r["library_id"] for r in roots])
    if removed is None:
        raise HTTPException(404, "link not found")
    return Response(status_code=204)
