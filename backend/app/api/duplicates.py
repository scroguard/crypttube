"""Admin: find files that are the same content under different names.

Files are compared by the fingerprint taken when they were indexed (size plus a hash of
the first and last 64 KiB), so finding duplicates costs one indexed query and no disk
reads. Only global libraries are looked at: users' own libraries stay private.

The media store is read-only, so nothing is deleted here. The workflow is: combine the
details of the copies (so no rating or tag is lost whichever copy is kept), download the
list of extra copies, delete those outside the app, then run Maintenance -> Clean up.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict

from app import counts
from app.api import browse

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
