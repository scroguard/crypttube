"""Admin: find files that are the same content under different names.

Files are compared by the fingerprint taken when they were indexed (size plus a hash of
the first and last 64 KiB), so finding duplicates costs one indexed query and no disk
reads. Only global libraries are looked at: users' own libraries stay private.

The media store is read-only, so nothing is deleted here. The workflow is: combine the
details of the copies (so no rating or tag is lost whichever copy is kept), download the
list of extra copies, delete those outside the app, then run Maintenance -> Clean up.
"""

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict

from app import counts, jobs
from app.api import browse
from app.config import get_settings
from app.deletion import DeleteError, delete_content

JOB_KIND = "delete_duplicates"
log = logging.getLogger(__name__)

router = APIRouter(prefix="/duplicates", tags=["duplicates"])

Kind = Literal["scene", "image"]
TABLE = {"scene": "scenes", "image": "images"}
# Plain files (not zip members) that are present, fingerprinted and in a global library.
CANDIDATES = """files f JOIN library_paths l ON l.id = f.library_id JOIN {table} c ON c.file_id = f.id
                WHERE f.oshash IS NOT NULL AND f.zip_file_id IS NULL AND f.missing_since IS NULL
                  AND l.owner_id IS NULL"""
GROUPS = "SELECT f.oshash, f.size, count(*) AS n FROM " + CANDIDATES + " GROUP BY f.oshash, f.size HAVING count(*) > 1"


def _groups(kind: str) -> str:
    return GROUPS.format(table=TABLE[kind])


async def _summary(pool, kind: str) -> dict:
    row = await pool.fetchrow(
        f"""SELECT count(*) AS groups, coalesce(sum(n - 1), 0)::bigint AS extra_copies,
                   coalesce(sum(size * (n - 1)), 0)::bigint AS extra_bytes FROM ({_groups(kind)}) g""")
    return dict(row)


@router.get("")
async def list_duplicates(request: Request, kind: Kind = "scene", limit: int = Query(25, ge=1, le=100),
                          offset: int = Query(0, ge=0)) -> dict:
    """Groups of identical files, the ones wasting the most space first. In each group the
    copy indexed first is marked `keep` (a suggestion; any copy can be the one kept)."""
    pool = request.app.state.pool
    extra = "c.duration, c.width, c.height" if kind == "scene" else "NULL::real AS duration, c.width, c.height"
    rows = await pool.fetch(
        f"""WITH g AS ({_groups(kind)} ORDER BY f.size * (count(*) - 1) DESC, f.oshash LIMIT $1 OFFSET $2)
            SELECT g.oshash, g.size, c.id, c.title, c.rating, c.assets_rev, c.created_at, {extra},
                   f.id AS file_id, f.path, f.mod_time,
                   (SELECT name FROM studios WHERE id = c.studio_id) AS studio,
                   ARRAY(SELECT name FROM tags WHERE id = ANY(c.tag_ids) ORDER BY name) AS tags,
                   ARRAY(SELECT name FROM performers WHERE id = ANY(c.performer_ids) ORDER BY name) AS performers
            FROM g JOIN files f ON f.oshash = g.oshash AND f.size = g.size
            JOIN library_paths l ON l.id = f.library_id JOIN {TABLE[kind]} c ON c.file_id = f.id
            WHERE f.zip_file_id IS NULL AND f.missing_since IS NULL AND l.owner_id IS NULL
            ORDER BY g.size * (g.n - 1) DESC, g.oshash, f.id""", limit, offset)
    groups: list[dict] = []
    for r in rows:
        item = dict(r)
        if not groups or groups[-1]["oshash"] != item["oshash"] or groups[-1]["size"] != item["size"]:
            groups.append({"oshash": item["oshash"], "size": item["size"], "items": []})
        item["thumbnail_url"] = (browse.scene_thumb(item, 320) if kind == "scene" else
                                 browse.image_urls(item["id"], item["file_id"], item["mod_time"], 320,
                                                   item["assets_rev"])["thumbnail_url"])
        item["keep"] = not groups[-1]["items"]
        for k in ("oshash", "size", "assets_rev", "mod_time"):
            del item[k]
        groups[-1]["items"].append(item)
    for g in groups:
        details = {(i["rating"], i["studio"], tuple(i["tags"]), tuple(i["performers"])) for i in g["items"]}
        g["in_sync"] = len(details) == 1
    summary = await _summary(pool, kind)
    return {"groups": groups, "has_more": offset + len(groups) < summary["groups"],
            "summary": {k: await _summary(pool, k) for k in TABLE} | {kind: summary}}


async def _combine(conn, kind: str, ids: list[int]) -> None:
    """Give every copy the union of the group's tags and performers, and fill empty
    fields from a copy that has them. Afterwards any copy can be deleted without loss."""
    table = TABLE[kind]
    for link, col in ((f"{kind}_tags", "tag_id"), (f"{kind}_performers", "performer_id")):
        await conn.execute(
            f"""INSERT INTO {link} ({kind}_id, {col})
                SELECT c.id, x.{col} FROM unnest($1::bigint[]) AS c(id)
                CROSS JOIN (SELECT DISTINCT {col} FROM {link} WHERE {kind}_id = ANY($1::bigint[])) x
                ON CONFLICT DO NOTHING""", ids)
    fields = ["studio_id", "rating"] + (["details", "release_date"] if kind == "scene" else [])
    sets = ", ".join(
        f"{f} = coalesce(c.{f}, (SELECT o.{f} FROM {table} o WHERE o.id = ANY($1::bigint[]) "
        f"AND o.{f} IS NOT NULL ORDER BY o.id LIMIT 1))" for f in fields)
    await conn.execute(
        f"""UPDATE {table} c SET {sets},
                   organized = (SELECT bool_or(o.organized) FROM {table} o WHERE o.id = ANY($1::bigint[]))
            WHERE c.id = ANY($1::bigint[])""", ids)


class Combine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind
    id: int | None = None   # any copy of the group to combine; None = every group


@router.post("/combine")
async def combine(body: Combine, request: Request) -> dict:
    pool = request.app.state.pool
    table = TABLE[body.kind]
    candidates = CANDIDATES.format(table=table)
    async with pool.acquire() as conn, conn.transaction():
        if body.id is not None:
            key = await conn.fetchrow(f"SELECT f.oshash, f.size FROM {candidates} AND c.id = $1", body.id)
            if key is None:
                raise HTTPException(404, "not found")
            rows = await conn.fetch(
                f"SELECT array_agg(c.id ORDER BY c.id) AS ids FROM {candidates} AND f.oshash = $1 AND f.size = $2",
                key["oshash"], key["size"])
        else:
            rows = await conn.fetch(
                f"""SELECT array_agg(c.id ORDER BY c.id) AS ids FROM {candidates}
                    GROUP BY f.oshash, f.size HAVING count(*) > 1""")
        groups = [r["ids"] for r in rows if r["ids"] and len(r["ids"]) > 1]
        for ids in groups:
            await _combine(conn, body.kind, ids)
    await counts.request_refresh(pool)
    return {"groups": len(groups)}


COMPARE_CHUNK = 8 * 1024 * 1024


async def identical(a: str, b: str, progress=None) -> bool:
    """Byte-for-byte comparison, in large pieces: alternating small reads between two
    files makes a hard disk or a NAS seek back and forth constantly, which can turn a
    few seconds into hours. `progress(done_bytes)` is awaited after each piece."""
    def read_pair(fa, fb):
        return fa.read(COMPARE_CHUNK), fb.read(COMPARE_CHUNK)
    try:
        fa, fb = await asyncio.to_thread(open, a, "rb"), await asyncio.to_thread(open, b, "rb")
    except OSError:
        return False
    try:
        done = 0
        while True:
            x, y = await asyncio.to_thread(read_pair, fa, fb)
            if x != y:
                return False
            if not x:
                return True
            done += len(x)
            if progress:
                await progress(done)
    except OSError:
        return False
    finally:
        fa.close()
        fb.close()


async def delete_extra_copy(pool, kind: str, content_id: int, keeper_id: int | None = None, progress=None) -> dict:
    """Delete one copy of a duplicated file. First its details are combined into the
    other copies, and it is compared byte for byte with one that stays: the fingerprint
    only looks at the ends of a file, and a deletion must not rest on that alone."""
    table = TABLE[kind]
    candidates = CANDIDATES.format(table=table)
    rows = await pool.fetch(
        f"""SELECT c.id, f.path FROM {candidates} AND (f.oshash, f.size) =
                (SELECT f2.oshash, f2.size FROM files f2 JOIN {table} c2 ON c2.file_id = f2.id WHERE c2.id = $1)
            ORDER BY f.id""", content_id)
    target = next((r for r in rows if r["id"] == content_id), None)
    others = [r for r in rows if r["id"] != content_id]
    if target is None:
        raise HTTPException(404, "not found")
    if not others:
        raise HTTPException(409, "this is the only copy left")
    # It must be byte-identical to a copy that stays (any one: a set can also contain a
    # different file that merely shares the fingerprint).
    # The job names the copy it keeps, so "Suggested to keep" is always the survivor.
    for other in [o for o in others if keeper_id is None or o["id"] == keeper_id]:
        if await identical(target["path"], other["path"], progress):
            break
    else:
        raise HTTPException(409, "the files are not identical byte for byte; nothing was deleted")
    async with pool.acquire() as conn, conn.transaction():
        await _combine(conn, kind, [r["id"] for r in rows])
    result = await delete_content(pool, get_settings().generated_dir, table, [content_id], None)
    if not result["deleted"]:
        raise HTTPException(409, result["skipped"][0]["reason"] if result["skipped"] else "nothing was deleted")
    return result


class DeleteCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind
    id: int


@router.post("/delete")
async def delete_copy(body: DeleteCopy, request: Request) -> dict:
    try:
        return await delete_extra_copy(request.app.state.pool, body.kind, body.id)
    except DeleteError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/delete-set")
async def delete_set(body: DeleteCopy, request: Request) -> dict:
    """Delete every extra copy in the set that `id` belongs to, keeping the first-indexed
    one ("Suggested to keep"). Copies not byte-identical to the keeper are left alone."""
    pool = request.app.state.pool
    table = TABLE[body.kind]
    rows = await pool.fetch(
        f"""SELECT c.id FROM {CANDIDATES.format(table=table)} AND (f.oshash, f.size) =
                (SELECT f2.oshash, f2.size FROM files f2 JOIN {table} c2 ON c2.file_id = f2.id WHERE c2.id = $1)
            ORDER BY f.id""", body.id)
    if not rows:
        raise HTTPException(404, "not found")
    keeper, extras = rows[0]["id"], [r["id"] for r in rows[1:]]
    deleted = freed = 0
    kept: list[str] = []
    for content_id in extras:
        try:
            result = await delete_extra_copy(pool, body.kind, content_id, keeper)
            deleted += 1
            freed += result["bytes"]
        except DeleteError as e:
            raise HTTPException(409, str(e)) from e
        except HTTPException as e:
            kept.append(str(e.detail))
    return {"deleted": deleted, "bytes": freed, "kept": len(kept), "reason": kept[0] if kept else None}


class DeleteExtras(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind


@router.post("/delete-extras", status_code=202)
async def delete_extras(body: DeleteExtras, request: Request) -> dict:
    """Queue a job that deletes every extra copy (all but the first-indexed of each set)."""
    return {"job_id": await jobs.enqueue(request.app.state.pool, JOB_KIND, {"kind": body.kind},
                                         dedupe_key=f"{JOB_KIND}:{body.kind}")}


async def run_delete_extras(pool, ctx, kind: str) -> str:
    rows = await pool.fetch(
        f"""SELECT array_agg(c.id ORDER BY f.id) AS ids, f.size FROM {CANDIDATES.format(table=TABLE[kind])}
            GROUP BY f.oshash, f.size HAVING count(*) > 1""")
    extras = [(i, r["ids"][0], r["size"]) for r in rows for i in r["ids"][1:]]
    total_bytes = sum(size for _, _, size in extras) or 1
    log.info("deleting duplicates: %d extra %s copies to check (%.1f GB)", len(extras), kind, total_bytes / 2**30)
    checked_bytes = 0
    deleted = freed = different = failed = 0
    reason = None
    for n, (content_id, keeper_id, size) in enumerate(extras, start=1):
        if await ctx.cancelled():
            break

        async def progress(done: int, n=n, size=size) -> None:
            await ctx.progress((checked_bytes + done) / total_bytes,
                               f"comparing copy {n} of {len(extras)}: {done / 2**30:.1f} of {size / 2**30:.1f} GB"
                               f" · {deleted} deleted so far")
            if await ctx.cancelled():
                raise asyncio.CancelledError
        try:
            result = await delete_extra_copy(pool, kind, content_id, keeper_id, progress)
            deleted += 1
            freed += result["bytes"]
            log.info("deleted duplicate %s %s (%d bytes)", kind, content_id, result["bytes"])
        except asyncio.CancelledError:
            break
        except HTTPException as e:
            if "not identical" in str(e.detail):
                different += 1
            else:
                failed += 1
                reason = reason or str(e.detail)
        checked_bytes += size
        await ctx.progress(checked_bytes / total_bytes, f"{n} of {len(extras)} copies checked · {deleted} deleted")
    message = f"deleted {deleted} extra copies ({freed / 1_048_576:.1f} MB freed)"
    if different:
        message += f"; {different} kept because they are not identical byte for byte"
    if failed:
        message += f"; {failed} could not be deleted ({reason})"
    return message


@router.get("/job")
async def delete_job(request: Request, kind: Kind = "scene") -> dict | None:
    """The latest "delete all extra copies" job for this kind, to show its progress and result."""
    row = await request.app.state.pool.fetchrow(
        """SELECT id, status, progress, message, error, created_at, finished_at FROM jobs
           WHERE kind = $1 AND payload->>'kind' = $2 ORDER BY id DESC LIMIT 1""", JOB_KIND, kind)
    return dict(row) if row else None


@router.get("/extra-copies.txt", response_class=PlainTextResponse)
async def extra_copies(request: Request, kind: Kind = "scene") -> PlainTextResponse:
    """One path per line: every copy except the first-indexed one of each group."""
    rows = await request.app.state.pool.fetch(
        f"""SELECT path FROM (
                SELECT f.path, row_number() OVER (PARTITION BY f.oshash, f.size ORDER BY f.id) AS rn,
                       count(*) OVER (PARTITION BY f.oshash, f.size) AS n
                FROM {CANDIDATES.format(table=TABLE[kind])}) x
            WHERE n > 1 AND rn > 1 ORDER BY path""")
    return PlainTextResponse("".join(r["path"] + "\n" for r in rows), headers={
        "Content-Disposition": f'attachment; filename="duplicate-{TABLE[kind]}.txt"'})
