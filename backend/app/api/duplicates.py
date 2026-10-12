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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Comparison:
    same: bool
    reason: str = ""      # why not: where the files differ, or what couldn't be read
    # which side has only empty (zero) bytes where they differ: typical of a damaged copy
    empty_in: str | None = None   # "a", "b" or None


def _read_exactly(f, n: int) -> bytes:
    """Read n bytes (fewer only at the end of the file). A single read may return less
    than asked on network filesystems (SMB, NFS, FUSE) without being at the end."""
    parts, got = [], 0
    while got < n:
        part = f.read(n - got)
        if not part:
            break
        parts.append(part)
        got += len(part)
    return b"".join(parts)


def _first_difference(x: bytes, y: bytes) -> int:
    lo, hi = 0, min(len(x), len(y))
    while lo < hi:                       # binary search over equal prefixes
        mid = (lo + hi) // 2
        if x[:mid + 1] == y[:mid + 1]:
            lo = mid + 1
        else:
            hi = mid
    return lo


async def identical(a: str, b: str, progress=None) -> Comparison:
    """Byte-for-byte comparison, in large pieces: alternating small reads between two
    files makes a hard disk or a NAS seek back and forth constantly, which can turn a
    few seconds into hours. `progress(done_bytes)` is awaited after each piece."""
    def read_pair(fa, fb):
        return _read_exactly(fa, COMPARE_CHUNK), _read_exactly(fb, COMPARE_CHUNK)

    opened = []
    try:
        for path in (a, b):
            try:
                opened.append(await asyncio.to_thread(open, path, "rb"))
            except OSError as e:
                return Comparison(False, f"couldn't open {path}: {e.strerror or e}")
        fa, fb = opened
        done = 0
        while True:
            try:
                x, y = await asyncio.to_thread(read_pair, fa, fb)
            except OSError as e:
                return Comparison(False, f"couldn't read after {done:,} bytes: {e.strerror or e}")
            if x != y:
                if len(x) != len(y):
                    return Comparison(False, f"one file ends early ({done + min(len(x), len(y)):,} bytes)")
                off = _first_difference(x, y)
                at = done + off
                empty = bytes(min(4096, len(x) - off))
                empty_in = "a" if x[off:off + len(empty)] == empty else "b" if y[off:off + len(empty)] == empty else None
                return Comparison(False, f"contents differ from byte {at:,}", empty_in)
            if not x:
                return Comparison(True)
            done += len(x)
            if progress:
                await progress(done)
    finally:
        for f in opened:
            f.close()


async def delete_extra_copy(pool, kind: str, content_id: int, keeper_id: int | None = None, progress=None,
                            force: bool = False) -> dict:
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
    reasons = []
    for other in ([] if force else [o for o in others if keeper_id is None or o["id"] == keeper_id]):
        result = await identical(target["path"], other["path"], progress)
        if result.same:
            break
        reasons.append(result.reason)
        log.info("not deleting %s: compared with %s: %s", target["path"], other["path"], result.reason)
    else:
        if not force:
            raise HTTPException(409, "the files are not identical byte for byte; nothing was deleted"
                                     + (f" ({reasons[0]})" if reasons else ""))
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
    # Delete even if not byte-identical to another copy (the admin looked and decided).
    force: bool = False


@router.post("/delete")
async def delete_copy(body: DeleteCopy, request: Request) -> dict:
    try:
        return await delete_extra_copy(request.app.state.pool, body.kind, body.id, force=body.force)
    except DeleteError as e:
        raise HTTPException(409, str(e)) from e


async def clean_set(pool, kind: str, any_id: int, progress=None) -> dict:
    """Delete the extra copies in the set `any_id` belongs to, keeping one.

    The copies are grouped by identical content (byte for byte), and one copy of the
    largest group is kept, preferring the first indexed ("Suggested to keep"). The rest of
    that group is deleted. A copy that matches none of them (often a damaged one, or a
    different file with the same fingerprint) is left alone and reported."""
    table = TABLE[kind]
    rows = await pool.fetch(
        f"""SELECT c.id, f.path, f.size FROM {CANDIDATES.format(table=table)} AND (f.oshash, f.size) =
                (SELECT f2.oshash, f2.size FROM files f2 JOIN {table} c2 ON c2.file_id = f2.id WHERE c2.id = $1)
            ORDER BY f.id""", any_id)
    if not rows:
        raise HTTPException(404, "not found")
    groups: list[list] = []
    verdicts: dict[tuple[int, int], Comparison] = {}       # (copy, group's first copy) -> result
    for row in rows:
        for group in groups:
            result = await identical(row["path"], group[0]["path"], progress)
            verdicts[(row["id"], group[0]["id"])] = result
            if result.same:
                group.append(row)
                break
        else:
            groups.append([row])
    winner = max(groups, key=len)           # max() keeps the first of equals: the earliest indexed
    keeper = winner[0]

    def why(row) -> str:
        rep = next(g[0] for g in groups if row in g)
        if (v := verdicts.get((rep["id"], keeper["id"]))) is not None:
            this_side = "a"                  # compared as identical(this copy, kept copy)
        elif (v := verdicts.get((keeper["id"], rep["id"]))) is not None:
            this_side = "b"
        else:
            return "differs from the copy kept"
        if v.empty_in == this_side:
            return f"{v.reason}; this copy has empty bytes there, so it is probably damaged"
        if v.empty_in:
            return f"{v.reason}; the kept copy has empty bytes there"
        return v.reason

    async with pool.acquire() as conn, conn.transaction():
        await _combine(conn, kind, [r["id"] for r in rows])
    extras = [r["id"] for r in winner[1:]]
    result = await delete_content(pool, get_settings().generated_dir, table, extras, None) if extras else \
        {"deleted": 0, "bytes": 0, "skipped": []}
    kept = [{"id": r["id"], "path": r["path"], "reason": why(r)} for g in groups if g is not winner for r in g]
    for k in kept:
        log.info("kept %s: %s", k["path"], k["reason"])
    return {"deleted": result["deleted"], "bytes": result["bytes"], "kept": kept,
            "skipped": result["skipped"], "keeper": {"id": keeper["id"], "path": keeper["path"]},
            "suggested_kept": keeper["id"] == rows[0]["id"]}


@router.post("/delete-set")
async def delete_set(body: DeleteCopy, request: Request) -> dict:
    """Delete the extra copies of one set (see clean_set)."""
    try:
        r = await clean_set(request.app.state.pool, body.kind, body.id)
    except DeleteError as e:
        raise HTTPException(409, str(e)) from e
    return {"deleted": r["deleted"], "bytes": r["bytes"], "kept": len(r["kept"]),
            "reason": r["kept"][0]["reason"] if r["kept"] else None,
            "kept_copies": r["kept"], "keeper": r["keeper"], "suggested_kept": r["suggested_kept"]}


class DeleteExtras(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind


@router.post("/delete-extras", status_code=202)
async def delete_extras(body: DeleteExtras, request: Request) -> dict:
    """Queue a job that deletes every extra copy (all but the first-indexed of each set)."""
    return {"job_id": await jobs.enqueue(request.app.state.pool, JOB_KIND, {"kind": body.kind},
                                         dedupe_key=f"{JOB_KIND}:{body.kind}")}


async def run_delete_extras(pool, ctx, kind: str) -> str:
    sets = await pool.fetch(
        f"""SELECT min(c.id) AS any_id, count(*) AS n, f.size FROM {CANDIDATES.format(table=TABLE[kind])}
            GROUP BY f.oshash, f.size HAVING count(*) > 1 ORDER BY min(f.id)""")
    # every extra copy is read once against the copy kept, roughly
    total_bytes = sum(r["size"] * (r["n"] - 1) for r in sets) or 1
    log.info("deleting duplicates: %d %s sets to check (%.1f GB to compare)", len(sets), kind, total_bytes / 2**30)
    checked = deleted = freed = kept = failed = 0
    why = reason = None
    for n, row in enumerate(sets, start=1):
        if await ctx.cancelled():
            break
        base = checked

        async def progress(done: int, n=n, base=base) -> None:
            await ctx.progress(min(1.0, (base + done) / total_bytes),
                               f"set {n} of {len(sets)}: compared {done / 2**30:.1f} GB · {deleted} deleted so far")
            if await ctx.cancelled():
                raise asyncio.CancelledError
        try:
            r = await clean_set(pool, kind, row["any_id"], progress)
        except asyncio.CancelledError:
            break
        except HTTPException as e:
            failed += 1
            reason = reason or str(e.detail)
            continue
        deleted += r["deleted"]
        freed += r["bytes"]
        kept += len(r["kept"])
        why = why or (r["kept"][0]["reason"] if r["kept"] else None)
        if r["skipped"]:
            failed += len(r["skipped"])
            reason = reason or r["skipped"][0]["reason"]
        checked += row["size"] * (row["n"] - 1)
        await ctx.progress(min(1.0, checked / total_bytes), f"{n} of {len(sets)} sets done · {deleted} deleted")
    message = f"deleted {deleted} extra copies ({freed / 1_048_576:.1f} MB freed)"
    if kept:
        message += f"; {kept} kept because they differ from the other copies (e.g. {why})"
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
