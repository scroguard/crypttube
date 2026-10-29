"""Deleting media for good: the file on disk, its database entries and what was generated
for it. Admins delete in global libraries; a user deletes in their own libraries only.

The file is removed first; only when that worked are the database rows deleted. So a
media store that is mounted read-only fails cleanly with nothing changed.
"""

import asyncio
import errno
import os
from pathlib import Path

import asyncpg

from app import counts, generated
from app.acl import Acl, visible
from app.sql import Q

KINDS = {"scenes": "scene", "images": "image", "galleries": "gallery"}


class DeleteError(Exception):
    """Nothing could be deleted, for a reason the admin should see."""


def _unlink(paths: list[str]) -> list[str]:
    """Remove files; returns the ones removed (or already gone)."""
    done = []
    for path in paths:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            if e.errno in (errno.EROFS, errno.EACCES, errno.EPERM) and not done:
                raise DeleteError(
                    "the media store can't be written to, so the file was not deleted. Mount it read-write "
                    "(MEDIA_MODE=rw) and make sure the app's user (PUID) may delete files there.") from e
            break   # keep what was done consistent; the rest is reported as skipped
        done.append(path)
    return done


def _prune_empty_dirs(items: list[tuple[str, str]]) -> None:
    for path, root in items:
        parent, top = Path(path).parent, Path(root)
        while parent != top and parent.is_relative_to(top):
            try:
                parent.rmdir()          # only succeeds when empty
            except OSError:
                break
            parent = parent.parent


async def _file_rows(pool, kind: str, ids: list[int], acl: Acl | None, *, admin: bool,
                     user_id: int | None) -> tuple[list[asyncpg.Record], dict[int, str]]:
    """The plain files behind the given scenes/images/galleries, plus why any were skipped."""
    q = Q()
    alias = {"scenes": "c", "images": "c", "galleries": "g"}[kind]
    conds = [f"{alias}.id = ANY({q.p(ids)}::bigint[])"]
    if acl is not None:
        conds += visible(acl, KINDS[kind], alias, q)
    where = " AND ".join(conds)
    cols = "f.id AS file_id, f.path, f.oshash, f.size, f.mod_time, f.zip_file_id, l.owner_id, l.path AS library_path"
    if kind == "galleries":
        sql = f"""SELECT g.id, {cols} FROM galleries g JOIN files f ON f.id = g.zip_file_id
                  JOIN library_paths l ON l.id = f.library_id WHERE {where}
                  UNION ALL
                  SELECT g.id, {cols} FROM galleries g JOIN gallery_images gi ON gi.gallery_id = g.id
                  JOIN images i ON i.id = gi.image_id JOIN files f ON f.id = i.file_id
                  JOIN library_paths l ON l.id = f.library_id
                  WHERE {where} AND g.zip_file_id IS NULL AND f.zip_file_id IS NULL"""
    else:
        sql = f"""SELECT c.id, {cols} FROM {kind} c JOIN files f ON f.id = c.file_id
                  JOIN library_paths l ON l.id = f.library_id WHERE {where}"""
    rows = await pool.fetch(sql, *q.args)
    skipped: dict[int, str] = {}
    keep = []
    found = {r["id"] for r in rows}
    if kind == "galleries":   # an empty or all-archive gallery has no rows above
        q2 = Q()
        conds2 = [f"g.id = ANY({q2.p(ids)}::bigint[])"] + (visible(acl, "gallery", "g", q2) if acl is not None else [])
        found |= {r["id"] for r in await pool.fetch(f"SELECT g.id FROM galleries g WHERE {' AND '.join(conds2)}", *q2.args)}
    for i in ids:
        if i not in found:
            skipped[i] = "not found"
    for r in rows:
        if r["owner_id"] is None and not admin:
            skipped[r["id"]] = "only admins can delete from the main libraries"
        elif r["owner_id"] is not None and r["owner_id"] != user_id:
            skipped[r["id"]] = "it belongs to someone else's library"
        elif kind != "galleries" and r["zip_file_id"] is not None:
            skipped[r["id"]] = "it is inside an archive; delete the gallery instead"
        elif not r["path"].startswith(r["library_path"].rstrip("/") + "/"):
            skipped[r["id"]] = "its path is outside its library"
        else:
            keep.append(r)
    return keep, skipped


async def delete_content(pool: asyncpg.Pool, generated_dir: Path, kind: str, ids: list[int],
                         acl: Acl | None, *, admin: bool = True, user_id: int | None = None) -> dict:
    """Delete the files of the given scenes/images/galleries, then their rows and
    generated assets. `acl` limits it to what that viewer can see (None = no limit)."""
    rows, skipped = await _file_rows(pool, kind, list(dict.fromkeys(ids)), acl, admin=admin, user_id=user_id)
    by_path = {r["path"]: r for r in rows}
    removed = await asyncio.to_thread(_unlink, list(by_path))
    for path, r in by_path.items():
        if path not in removed:
            skipped[r["id"]] = "the file could not be deleted"
    gone = [by_path[p] for p in removed]
    # In a user's own library, folders left empty go too (as when deleting there by path).
    await asyncio.to_thread(_prune_empty_dirs, [(r["path"], r["library_path"]) for r in gone if r["owner_id"] is not None])
    file_ids = [r["file_id"] for r in gone]
    if file_ids:
        # Generated files: image thumbnails are per file; scene assets are shared by
        # identical files, so they go only when no other copy is left.
        members = await pool.fetch(
            "SELECT id, mod_time FROM files WHERE zip_file_id = ANY($1::bigint[])", file_ids)
        assets: list[Path] = []
        for file_id, mod_time in [(r["file_id"], r["mod_time"]) for r in gone] + [(m["id"], m["mod_time"]) for m in members]:
            assets += generated.image_asset_files(generated_dir, generated.image_version(file_id, mod_time))
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM files WHERE id = ANY($1::bigint[])", file_ids)   # cascades
            if kind == "galleries":
                await conn.execute("DELETE FROM galleries WHERE id = ANY($1::bigint[]) AND image_count = 0",
                                   list({r["id"] for r in gone}))
            await conn.execute("DELETE FROM galleries WHERE folder_path IS NOT NULL AND image_count = 0")
            await conn.execute(
                """UPDATE galleries g SET cover_image_id = (
                       SELECT image_id FROM gallery_images WHERE gallery_id = g.id ORDER BY position, image_id LIMIT 1)
                   WHERE g.cover_image_id IS NULL AND g.image_count > 0""")
            hashes = {r["oshash"]: r["file_id"] for r in gone if r["oshash"]}
            still = {r["oshash"] for r in await conn.fetch(
                "SELECT DISTINCT oshash FROM files WHERE oshash = ANY($1::text[])", list(hashes))}
        for r in gone:
            if r["oshash"] not in still:
                assets += generated.scene_asset_files(generated_dir, generated.scene_version(r["oshash"], r["file_id"]))
        await asyncio.to_thread(generated.remove_files, assets)
        await counts.request_refresh(pool)
    deleted_ids = {r["id"] for r in gone} - set(skipped)
    return {"deleted": len(deleted_ids), "files": len(gone), "bytes": sum(r["size"] for r in gone),
            "skipped": [{"id": i, "reason": reason} for i, reason in sorted(skipped.items())]}
