"""Move scenes and images to another folder: the file on disk and its entry, keeping all
details (ratings, tags, performers, play history, previews).

Admins move within and between the main libraries; a user moves within and between
their own libraries. Never between the two. A Live Photo's video moves with its photo.
Folder galleries follow: a moved picture leaves its old folder's gallery and joins the
new folder's.
"""

import asyncio
import errno
import os
import re
import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app import counts, folder_links
from app.acl import visible
from app.auth import User, require_user
from app.config import get_settings
from app.deletion import _prune_empty_dirs
from app.generated import DIR_MODE
from app.scanner import Scanner, pair_live_photos
from app.sql import Q

router = APIRouter(prefix="/move", tags=["move"])

KINDS = {"scenes": "scene", "images": "image"}
MAX_NAME = 200
BROWSE_LIMIT = 5000


async def _roots(pool, user: User) -> list[dict]:
    """The libraries this user may move files within: the main ones for admins, and
    everyone's own."""
    rows = await pool.fetch(
        """SELECT id, path, name, owner_id FROM library_paths
           WHERE ($1 AND owner_id IS NULL) OR owner_id = $2
           ORDER BY owner_id NULLS FIRST, lower(coalesce(name, path))""", user.is_admin, user.id)
    return [{"library_id": r["id"], "path": r["path"], "owner_id": r["owner_id"],
             "name": r["name"] if r["owner_id"] is not None else r["path"],
             "mine": r["owner_id"] is not None} for r in rows]


def _root_for(roots: list[dict], path: Path) -> dict | None:
    """The library containing `path` (the deepest, should libraries ever nest)."""
    matches = [r for r in roots if path == Path(r["path"]) or path.is_relative_to(r["path"])]
    return max(matches, key=lambda r: len(r["path"]), default=None)


def _check_dir(raw: str, roots: list[dict]) -> tuple[Path, dict]:
    if not raw.startswith("/"):
        raise HTTPException(422, "path must be absolute")
    path = Path(raw).resolve()
    root = _root_for(roots, path)
    if root is None:
        raise HTTPException(422, "that folder is not in a library you can move files to")
    if not path.is_dir():
        raise HTTPException(404, "folder not found")
    return path, root


def _subfolders(path: Path, skip: Path) -> list[str]:
    names = []
    with os.scandir(path) as it:
        for e in it:
            if e.name.startswith(".") or not e.is_dir(follow_symlinks=False) or Path(e.path) == skip:
                continue
            names.append(e.name)
    return sorted(names, key=lambda n: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", n)])


@router.get("/folders")
async def folders(request: Request, path: str | None = None, user: User = Depends(require_user)) -> dict:
    """Browse where files can be moved to. Without `path`: the libraries you can use."""
    roots = await _roots(request.app.state.pool, user)
    if path is None:
        return {"path": None, "parent": None, "library": None, "entries": [
            {"name": r["name"], "path": r["path"], "mine": r["mine"]} for r in roots]}
    folder, root = _check_dir(path, roots)
    try:
        names = await asyncio.to_thread(_subfolders, folder, get_settings().generated_dir.resolve())
    except PermissionError:
        raise HTTPException(403, "folder is not readable by the server") from None
    at_root = folder == Path(root["path"])
    rel = "" if at_root else str(folder.relative_to(root["path"]))
    return {"path": str(folder), "parent": None if at_root else str(folder.parent),
            "library": {"name": root["name"], "path": root["path"], "mine": root["mine"]},
            "relative": rel, "truncated": len(names) > BROWSE_LIMIT,
            "entries": [{"name": n, "path": str(folder / n)} for n in names[:BROWSE_LIMIT]]}


class MoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["scenes", "images"]
    ids: list[int] = Field(min_length=1, max_length=5000)
    dest: str = Field(min_length=1, max_length=4000)        # an existing folder...
    new_folder: str | None = Field(None, max_length=1000)   # ...or a new one inside it ("a/b" allowed)
    # When a file with the same name is already there: report it so the user can choose
    # a name ("ask"), add a number to the name ("rename"), or leave the item ("skip").
    on_conflict: Literal["ask", "rename", "skip"] = "ask"
    # Names chosen by the user, per item id (the extension is kept whatever is typed).
    names: dict[int, str] = Field(default_factory=dict)


def _new_folder_parts(raw: str) -> list[str]:
    parts = [p for p in raw.replace("\\", "/").split("/") if p.strip()]
    if not parts:
        raise HTTPException(422, "the new folder needs a name")
    for p in parts:
        if (p in (".", "..") or p.startswith(".") or len(p) > MAX_NAME or p != p.strip()
                or re.search(r"[\x00-\x1f\x7f]", p)):
            raise HTTPException(422, f"invalid folder name: {p!r}")
    return parts


def _make_dirs(base: Path, parts: list[str]) -> Path:
    path = base
    for p in parts:
        path = path / p
        try:
            path.mkdir(mode=DIR_MODE)
            os.chmod(path, DIR_MODE)
        except FileExistsError:
            if not path.is_dir():
                raise HTTPException(409, f"{p} exists and is not a folder") from None
    return path


def _clean_name(raw: str) -> str:
    name = raw.strip()
    if (not name or name in (".", "..") or name.startswith(".") or "/" in name or "\\" in name
            or len(name) > MAX_NAME or re.search(r"[\x00-\x1f\x7f]", name)):
        raise HTTPException(422, f"invalid file name: {raw!r}")
    return name


def _free_stem(dest: Path, stem: str, exts: list[str]) -> str:
    """`stem`, or `stem (2)`, `stem (3)`… : the first under which every one of `exts` is free."""
    candidate, n = stem, 1
    while any(os.path.lexists(dest / f"{candidate}{ext}") for ext in exts):
        n += 1
        candidate = f"{stem} ({n})"
    return candidate


def _move_file(src: str, dst: str) -> None:
    """Rename; across filesystems copy (keeping times) then remove. Never overwrites."""
    if os.path.lexists(dst):
        raise FileExistsError(dst)
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.copy2(src, dst)
        os.unlink(src)


@router.post("")
async def move(body: MoveRequest, request: Request, user: User = Depends(require_user)) -> dict:
    pool = request.app.state.pool
    roots = await _roots(pool, user)
    dest, dest_root = _check_dir(body.dest, roots)
    if body.new_folder:
        dest = await asyncio.to_thread(_make_dirs, dest, _new_folder_parts(body.new_folder))

    kind = KINDS[body.kind]
    q = Q()
    conds = [f"c.id = ANY({q.p(list(dict.fromkeys(body.ids)))}::bigint[])", *visible(user.acl, kind, "c", q)]
    live = ", c.live_scene_id" if kind == "image" else ", NULL::bigint AS live_scene_id"
    rows = await pool.fetch(
        f"""SELECT c.id, f.id AS file_id, f.path, f.zip_file_id, l.id AS library_id, l.owner_id {live}
            FROM {body.kind} c JOIN files f ON f.id = c.file_id JOIN library_paths l ON l.id = f.library_id
            WHERE {' AND '.join(conds)}""", *q.args)
    found = {r["id"] for r in rows}
    skipped = {i: "not found" for i in body.ids if i not in found}
    allowed = {r["library_id"] for r in roots}

    scanner = Scanner(pool, get_settings().generated_dir)
    dest_lib = await pool.fetchrow("SELECT id, create_folder_galleries FROM library_paths WHERE id = $1",
                                   dest_root["library_id"])
    moved, touched_libs, user_dirs = 0, {dest_root["library_id"]}, []
    conflicts: list[dict] = []
    for r in rows:
        if r["zip_file_id"] is not None:
            skipped[r["id"]] = "it is inside an archive"
        elif r["library_id"] not in allowed:
            skipped[r["id"]] = "you can't move files in that library"
        elif r["owner_id"] != dest_root["owner_id"]:
            skipped[r["id"]] = ("files can't move between the main libraries and a user's own"
                                if user.is_admin else "you can only move files within your own libraries")
        else:
            files = [(r["file_id"], r["path"])]
            if r["live_scene_id"]:                          # a Live Photo's video goes along
                v = await pool.fetchrow(
                    "SELECT s.file_id, f.path FROM scenes s JOIN files f ON f.id = s.file_id WHERE s.id = $1",
                    r["live_scene_id"])
                if v and Path(v["path"]).parent == Path(r["path"]).parent:
                    files.append((v["file_id"], v["path"]))
            outcome = await _move_one(pool, files, dest, dest_lib["id"], body.on_conflict, body.names.get(r["id"]))
            if isinstance(outcome, dict):
                conflicts.append({"id": r["id"], **outcome})
                continue
            if outcome:
                skipped[r["id"]] = outcome
                continue
            moved += 1
            touched_libs.add(r["library_id"])
            if r["owner_id"] is not None:
                user_dirs.append((r["path"], next(x["path"] for x in roots if x["library_id"] == r["library_id"])))
            if kind == "image":
                await _refile_in_galleries(pool, scanner, r["id"], dest, dest_lib)
    await scanner._finalize_galleries()
    for lib in touched_libs:
        await pair_live_photos(pool, lib)
    if user_dirs:          # in users' own libraries, folders left empty go (as when deleting)
        await asyncio.to_thread(_prune_empty_dirs, user_dirs)
    if moved:
        await counts.request_refresh(pool)
    return {"moved": moved, "dest": str(dest), "conflicts": conflicts,
            "skipped": [{"id": i, "reason": why} for i, why in sorted(skipped.items())]}


async def _move_one(pool, files: list[tuple[int, str]], dest: Path, dest_lib: int,
                    on_conflict: str = "skip", chosen: str | None = None) -> str | dict | None:
    """Move the files (a photo, maybe with its video, which share a name) and update their
    entries. Returns None when moved, a reason when skipped, or for on_conflict="ask" a
    {"name", "suggested"} dict when the name is taken."""
    main = files[0][1]
    stem = Path(_clean_name(chosen)).stem if chosen else Path(main).stem
    exts = [Path(src).suffix for _, src in files]
    if all(Path(src).parent == dest and Path(src).stem == stem for _, src in files):
        return "it is already in that folder"
    taken = any(os.path.lexists(dest / f"{stem}{ext}") for ext in exts)
    if taken:
        if on_conflict == "rename":
            stem = _free_stem(dest, stem, exts)
        elif on_conflict == "ask":
            return {"name": f"{stem}{exts[0]}", "suggested": f"{_free_stem(dest, stem, exts)}{exts[0]}"}
        else:
            return "a file with that name is already in that folder"
    targets = [(fid, src, str(dest / f"{stem}{Path(src).suffix}")) for fid, src in files]
    done = []
    try:
        for fid, src, dst in targets:
            if src != dst:
                await asyncio.to_thread(_move_file, src, dst)
                done.append((fid, src, dst))
        async with pool.acquire() as conn, conn.transaction():
            for fid, src, dst in done:
                await conn.execute("UPDATE files SET path = $2, library_id = $3 WHERE id = $1", fid, dst, dest_lib)
                await folder_links.apply(conn, fid, src)
    except Exception as e:                         # put back what was moved, then report
        for _fid, src, dst in reversed(done):
            try:
                await asyncio.to_thread(_move_file, dst, src)
            except OSError:
                pass
        if isinstance(e, FileExistsError):
            return "a file with that name is already in that folder"
        if isinstance(e, PermissionError):
            return "the server isn't allowed to change that folder"
        if isinstance(e, OSError):
            return f"it couldn't be moved ({e.strerror or e})"
        raise
    return None


async def _refile_in_galleries(pool, scanner: Scanner, image_id: int, dest: Path, dest_lib) -> None:
    gallery_id = (await scanner._folder_gallery(str(dest), dest_lib["id"])
                  if dest_lib["create_folder_galleries"] else None)
    async with pool.acquire() as conn, conn.transaction():
        old = await conn.fetch(
            """DELETE FROM gallery_images gi USING galleries g
               WHERE gi.image_id = $1 AND gi.gallery_id = g.id AND g.folder_path IS NOT NULL
                 AND g.id IS DISTINCT FROM $2 RETURNING gi.gallery_id""", image_id, gallery_id)
        if gallery_id is not None:
            await conn.execute(
                "INSERT INTO gallery_images (gallery_id, image_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                gallery_id, image_id)
    scanner._affected_galleries.update([r["gallery_id"] for r in old] + ([gallery_id] if gallery_id else []))
