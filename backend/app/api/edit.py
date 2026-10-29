"""Write API: metadata editing, bulk edits, playback state, entity images.

PATCH bodies are partial: only fields present in the JSON are changed, and an explicit
null clears a field. Association fields (tag_ids, performer_ids, aliases, parent_ids)
replace the whole set.
"""

import asyncio
import io
from datetime import date
from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field

from app import counts, crypto, generated, jobs
from app.api import media as media_api
from app.api.libraries import media_file_path
from app.auth import require_admin
from app.fetch import FetchError, fetch_image_bytes
from app.permissions import (require_can_create, require_content_edit, require_entity_edit,
                             require_visible)
from app.acl import Acl, visible
from app.sql import Q
from app.auth import get_acl
from app.api import browse, entities
from app.config import get_settings
from app.generated import _atomic_write

# Both are mounted for any signed-in user; each write checks app.permissions
# (editors for global items, owners for their own).
router = APIRouter(tags=["edit"])
read_router = APIRouter(tags=["edit"])

Rating = Field(None, ge=0, le=100)
MAX_IMAGE_UPLOAD = 20 * 1024 * 1024
MAX_SOURCE_IMAGE = 100 * 1024 * 1024   # an original picked from the media store
ENTITY_IMAGE_MAX = 1024


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _db_error(e: asyncpg.PostgresError) -> HTTPException:
    if isinstance(e, asyncpg.UniqueViolationError):
        return HTTPException(409, "an item with that name already exists")
    if isinstance(e, asyncpg.ForeignKeyViolationError):
        return HTTPException(422, f"referenced item does not exist ({e.constraint_name})")
    if isinstance(e, asyncpg.CheckViolationError):
        return HTTPException(409 if "cycle" in str(e) else 422, str(e).split("\n")[0])
    raise e


async def _update(conn, table: str, row_id: int, fields: dict) -> bool:
    if not fields:
        return bool(await conn.fetchval(f"SELECT true FROM {table} WHERE id = $1", row_id))
    sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(fields, start=2))
    return await conn.fetchval(
        f"UPDATE {table} SET {sets} WHERE id = $1 RETURNING true", row_id, *fields.values()
    ) or False


async def _replace_links(conn, table: str, fk: str, col: str, owner_id: int, ids: list[int]) -> None:
    await conn.execute(f"DELETE FROM {table} WHERE {fk} = $1 AND {col} <> ALL($2::bigint[])", owner_id, ids)
    await conn.execute(
        f"INSERT INTO {table} ({fk}, {col}) SELECT $1, unnest($2::bigint[]) ON CONFLICT DO NOTHING",
        owner_id, ids,
    )


async def _write(request: Request, fn):
    """Run fn(conn) in a transaction, map constraint errors to HTTP, then queue a
    counts refresh (debounced, so a burst of edits costs one refresh)."""
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn, conn.transaction():
            result = await fn(conn)
    except asyncpg.PostgresError as e:
        raise _db_error(e) from e
    await counts.request_refresh(pool)
    return result


def _split(body: BaseModel, *assoc: str) -> tuple[dict, dict]:
    data = body.model_dump(exclude_unset=True)
    links = {k: data.pop(k) for k in assoc if k in data}
    for k, v in links.items():
        if v is None:
            raise HTTPException(422, f"{k} cannot be null; send [] to clear")
    return data, links


# ---------------------------------------------------------------------------
# Content: scenes, galleries, images
# ---------------------------------------------------------------------------

class ImagePatch(Body):
    title: str | None = None
    studio_id: int | None = None
    rating: int | None = Rating
    organized: bool | None = None
    custom_fields: dict | None = None
    tag_ids: list[int] | None = None
    performer_ids: list[int] | None = None


class GalleryPatch(ImagePatch):
    details: str | None = None
    release_date: date | None = None


class ScenePatch(GalleryPatch):
    urls: list[str] | None = None


KINDS = {
    "scenes": ("scene", browse.get_scene),
    "galleries": ("gallery", browse.get_gallery),
    "images": ("image", browse.get_image),
}


async def _patch_content(kind_path: str, item_id: int, body: ImagePatch, request: Request, acl: Acl):
    kind, getter = KINDS[kind_path]
    pool = request.app.state.pool
    await getter(item_id, request, acl)  # 404 if missing or hidden from this user
    await require_content_edit(pool, acl, kind_path, [item_id])
    data, links = _split(body, "tag_ids", "performer_ids")
    await require_visible(pool, acl, "tag", links.get("tag_ids", []))
    await require_visible(pool, acl, "performer", links.get("performer_ids", []))
    await require_visible(pool, acl, "studio", [data.get("studio_id")])
    if "custom_fields" in data and data["custom_fields"] is None:
        data["custom_fields"] = {}
    if "urls" in data and data["urls"] is None:
        data["urls"] = []

    async def fn(conn):
        await _update(conn, kind_path, item_id, data)
        if "tag_ids" in links:
            await _replace_links(conn, f"{kind}_tags", f"{kind}_id", "tag_id", item_id, links["tag_ids"])
        if "performer_ids" in links:
            await _replace_links(conn, f"{kind}_performers", f"{kind}_id", "performer_id", item_id,
                                 links["performer_ids"])

    await _write(request, fn)
    return await getter(item_id, request, acl)


@router.patch("/scenes/{scene_id}")
async def patch_scene(scene_id: int, body: ScenePatch, request: Request, acl: Acl = Depends(get_acl)):
    return await _patch_content("scenes", scene_id, body, request, acl)


@router.patch("/galleries/{gallery_id}")
async def patch_gallery(gallery_id: int, body: GalleryPatch, request: Request, acl: Acl = Depends(get_acl)):
    return await _patch_content("galleries", gallery_id, body, request, acl)


@router.patch("/images/{image_id}")
async def patch_image(image_id: int, body: ImagePatch, request: Request, acl: Acl = Depends(get_acl)):
    return await _patch_content("images", image_id, body, request, acl)


@router.post("/scenes/{scene_id}/regenerate", status_code=202)
async def regenerate_scene(scene_id: int, request: Request, acl: Acl = Depends(get_acl)) -> dict:
    """Rebuild this scene's screenshot, thumbnails and scrub sprite from the video."""
    pool = request.app.state.pool
    await browse.get_scene(scene_id, request, acl)
    await require_content_edit(pool, acl, "scenes", [scene_id])
    job_id = await jobs.enqueue(pool, "generate", {"scene_ids": [scene_id], "force": True},
                                dedupe_key=f"generate:force:scene:{scene_id}")
    return {"job_id": job_id}


class LinkEdit(Body):
    mode: Literal["add", "remove", "set"]
    ids: list[int]


class BulkSet(Body):
    studio_id: int | None = None
    rating: int | None = Rating
    organized: bool | None = None


class BulkEdit(Body):
    ids: list[int] = Field(min_length=1, max_length=10_000)
    set: BulkSet | None = None
    tags: LinkEdit | None = None
    performers: LinkEdit | None = None
    # Take these studios off the items that have them (others keep theirs).
    clear_studio_ids: list[int] | None = None


async def _bulk_links(conn, table: str, fk: str, col: str, owners: list[int], edit: LinkEdit) -> None:
    if edit.mode == "remove":
        await conn.execute(f"DELETE FROM {table} WHERE {fk} = ANY($1::bigint[]) AND {col} = ANY($2::bigint[])",
                           owners, edit.ids)
    elif edit.mode == "set":
        await conn.execute(f"DELETE FROM {table} WHERE {fk} = ANY($1::bigint[]) AND {col} <> ALL($2::bigint[])",
                           owners, edit.ids)
    if edit.mode in ("add", "set") and edit.ids:
        await conn.execute(
            f"""INSERT INTO {table} ({fk}, {col})
                SELECT o, t FROM unnest($1::bigint[]) o, unnest($2::bigint[]) t ON CONFLICT DO NOTHING""",
            owners, edit.ids,
        )


async def bulk_edit(kind_path: str, body: BulkEdit, request: Request, acl: Acl) -> dict:
    """Apply the same change to many items in a few set-based statements."""
    kind, _ = KINDS[kind_path]
    alias = {"scene": "s", "gallery": "g", "image": "i"}[kind]
    q = browse.Q()
    ids_ph = q.p(body.ids)
    cond = " AND ".join([f"{alias}.id = ANY({ids_ph}::bigint[])", *browse.visible(acl, kind, alias, q)])
    ids = [r["id"] for r in await request.app.state.pool.fetch(
        f"SELECT {alias}.id FROM {kind_path} {alias} WHERE {cond}", *q.args)]
    if len(ids) != len(set(body.ids)):
        raise HTTPException(404, "some items do not exist")
    pool = request.app.state.pool
    await require_content_edit(pool, acl, kind_path, ids)
    await require_visible(pool, acl, "tag", body.tags.ids if body.tags else [])
    await require_visible(pool, acl, "performer", body.performers.ids if body.performers else [])
    await require_visible(pool, acl, "studio", [body.set.studio_id if body.set else None])

    async def fn(conn):
        fields = body.set.model_dump(exclude_unset=True) if body.set else {}
        if fields:
            sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(fields, start=2))
            await conn.execute(f"UPDATE {kind_path} SET {sets} WHERE id = ANY($1::bigint[])", ids, *fields.values())
        if body.clear_studio_ids:
            await conn.execute(
                f"UPDATE {kind_path} SET studio_id = NULL WHERE id = ANY($1::bigint[]) AND studio_id = ANY($2::bigint[])",
                ids, body.clear_studio_ids)
        if body.tags:
            await _bulk_links(conn, f"{kind}_tags", f"{kind}_id", "tag_id", ids, body.tags)
        if body.performers:
            await _bulk_links(conn, f"{kind}_performers", f"{kind}_id", "performer_id", ids, body.performers)

    await _write(request, fn)
    return {"updated": len(ids)}


def _bulk_route(kind_path: str):
    async def route(body: BulkEdit, request: Request, acl: Acl = Depends(get_acl)) -> dict:
        return await bulk_edit(kind_path, body, request, acl)
    route.__name__ = f"bulk_edit_{kind_path}"
    return route


for _k in KINDS:
    router.add_api_route(f"/{_k}/bulk", _bulk_route(_k), methods=["POST"],
                         summary=f"Bulk edit {_k}")


class Selection(Body):
    ids: list[int] = Field(min_length=1, max_length=10_000)


async def assigned(kind_path: str, body: Selection, request: Request, acl: Acl) -> dict:
    """What a selection already has: each tag and performer on any of the items, with how
    many of the items carry it. Lets the bulk bar show (and remove) existing assignments."""
    kind, _ = KINDS[kind_path]
    out: dict = {}
    for key, col, table, entity in (("tags", "tag_ids", "tags", "tag"), ("performers", "performer_ids", "performers", "performer")):
        q = Q()
        ids = q.p(body.ids)
        content = " AND ".join([f"c.id = ANY({ids}::bigint[])", *visible(acl, kind, "c", q)])
        ent = " AND ".join(["e.id = x.id", *visible(acl, entity, "e", q)])
        rows = await request.app.state.pool.fetch(
            f"""SELECT e.id, e.name, x.n AS count
                FROM (SELECT unnest(c.{col}) AS id, count(*) AS n FROM {kind_path} c WHERE {content} GROUP BY 1) x
                JOIN {table} e ON {ent}
                ORDER BY x.n DESC, e.name COLLATE natural_sort""", *q.args)
        out[key] = [dict(r) for r in rows]
    q = Q()
    ids = q.p(body.ids)
    content = " AND ".join([f"c.id = ANY({ids}::bigint[])", *visible(acl, kind, "c", q)])
    ent = " AND ".join(["e.id = x.id", *visible(acl, "studio", "e", q)])
    rows = await request.app.state.pool.fetch(
        f"""SELECT e.id, e.name, x.n AS count
            FROM (SELECT c.studio_id AS id, count(*) AS n FROM {kind_path} c
                  WHERE {content} AND c.studio_id IS NOT NULL GROUP BY 1) x
            JOIN studios e ON {ent}
            ORDER BY x.n DESC, e.name COLLATE natural_sort""", *q.args)
    out["studios"] = [dict(r) for r in rows]
    return out


def _assigned_route(kind_path: str):
    async def route(body: Selection, request: Request, acl: Acl = Depends(get_acl)) -> dict:
        return await assigned(kind_path, body, request, acl)
    route.__name__ = f"assigned_{kind_path}"
    return route


for _k in KINDS:
    # POST only because a selection can be thousands of ids; it changes nothing.
    read_router.add_api_route(f"/{_k}/assigned", _assigned_route(_k), methods=["POST"],
                              summary=f"Tags and performers already on a selection of {_k}")


# ---------------------------------------------------------------------------
# Playback state
# ---------------------------------------------------------------------------

class Resume(Body):
    time: float | None = Field(ge=0)


@read_router.post("/scenes/{scene_id}/play", status_code=204)
async def record_play(scene_id: int, request: Request, acl: Acl = Depends(get_acl)) -> Response:
    await browse.get_scene(scene_id, request, acl)
    await request.app.state.pool.execute(
        "UPDATE scenes SET play_count = play_count + 1, last_played_at = now() WHERE id = $1", scene_id)
    return Response(status_code=204)


@read_router.put("/scenes/{scene_id}/resume", status_code=204)
async def set_resume(scene_id: int, body: Resume, request: Request, acl: Acl = Depends(get_acl)) -> Response:
    """Per-user resume position; null clears it (e.g. when playback finished)."""
    await browse.get_scene(scene_id, request, acl)
    pool = request.app.state.pool
    if body.time is None:
        await pool.execute("DELETE FROM user_scene_state WHERE user_id = $1 AND scene_id = $2",
                           acl.user_id, scene_id)
    else:
        await pool.execute(
            """INSERT INTO user_scene_state (user_id, scene_id, resume_time) VALUES ($1, $2, $3)
               ON CONFLICT (user_id, scene_id) DO UPDATE SET resume_time = $3, updated_at = now()""",
            acl.user_id, scene_id, body.time)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Performers, studios, tags
# ---------------------------------------------------------------------------

Gender = Literal["female", "male", "transgender_female", "transgender_male", "intersex", "non_binary"]


class PerformerBody(Body):
    name: str = Field(min_length=1, max_length=200)
    disambiguation: str | None = None
    gender: Gender | None = None
    birthdate: date | None = None
    deathdate: date | None = None
    country: str | None = None
    ethnicity: str | None = None
    eye_color: str | None = None
    hair_color: str | None = None
    height_cm: int | None = Field(None, gt=0, lt=300)
    weight_kg: int | None = Field(None, gt=0, lt=500)
    measurements: str | None = None
    tattoos: str | None = None
    piercings: str | None = None
    career_start: int | None = None
    career_end: int | None = None
    details: str | None = None
    rating: int | None = Rating
    custom_fields: dict = {}
    aliases: list[str] = []
    tag_ids: list[int] = []


class PerformerPatch(PerformerBody):
    name: str | None = Field(None, min_length=1, max_length=200)
    custom_fields: dict | None = None
    aliases: list[str] | None = None
    tag_ids: list[int] | None = None


async def _save_performer(conn, performer_id: int | None, data: dict, links: dict) -> int:
    if performer_id is None:
        cols = list(data)
        performer_id = await conn.fetchval(
            f"INSERT INTO performers ({', '.join(cols)}) VALUES ({', '.join(f'${i}' for i in range(1, len(cols) + 1))}) "
            "RETURNING id", *data.values())
    elif not await _update(conn, "performers", performer_id, data):
        raise HTTPException(404, "performer not found")
    if "aliases" in links:
        aliases = sorted({a.strip() for a in links["aliases"] if a.strip()})
        await conn.execute("DELETE FROM performer_aliases WHERE performer_id = $1", performer_id)
        await conn.executemany("INSERT INTO performer_aliases VALUES ($1, $2)", [(performer_id, a) for a in aliases])
    if "tag_ids" in links:
        await _replace_links(conn, "performer_tags", "performer_id", "tag_id", performer_id, links["tag_ids"])
    return performer_id


@router.post("/performers", status_code=201)
async def create_performer(body: PerformerBody, request: Request, acl: Acl = Depends(get_acl)):
    owner = require_can_create(acl)
    await require_visible(request.app.state.pool, acl, "tag", body.tag_ids)
    data = body.model_dump(exclude={"aliases", "tag_ids"}) | {"owner_id": owner}
    links = {"aliases": body.aliases, "tag_ids": body.tag_ids}
    pid = await _write(request, lambda conn: _save_performer(conn, None, data, links))
    return await entities.get_performer(pid, request, acl)


@router.patch("/performers/{performer_id}")
async def patch_performer(performer_id: int, body: PerformerPatch, request: Request, acl: Acl = Depends(get_acl)):
    await entities.get_performer(performer_id, request, acl)
    await require_entity_edit(request.app.state.pool, acl, "performer", [performer_id])
    data, links = _split(body, "aliases", "tag_ids")
    await require_visible(request.app.state.pool, acl, "tag", links.get("tag_ids", []))
    for k in ("name", "custom_fields"):
        if k in data and data[k] is None:
            raise HTTPException(422, f"{k} cannot be null")
    await _write(request, lambda conn: _save_performer(conn, performer_id, data, links))
    return await entities.get_performer(performer_id, request, acl)


class StudioBody(Body):
    name: str = Field(min_length=1, max_length=200)
    parent_id: int | None = None
    url: str | None = None
    details: str | None = None
    rating: int | None = Rating
    custom_fields: dict = {}


class StudioPatch(StudioBody):
    name: str | None = Field(None, min_length=1, max_length=200)
    custom_fields: dict | None = None


@router.post("/studios", status_code=201)
async def create_studio(body: StudioBody, request: Request, acl: Acl = Depends(get_acl)):
    owner = require_can_create(acl)
    await require_visible(request.app.state.pool, acl, "studio", [body.parent_id])
    data = body.model_dump() | {"owner_id": owner}
    cols = list(data)
    sid = await _write(request, lambda conn: conn.fetchval(
        f"INSERT INTO studios ({', '.join(cols)}) VALUES ({', '.join(f'${i}' for i in range(1, len(cols) + 1))}) "
        "RETURNING id", *data.values()))
    return await entities.get_studio(sid, request, acl)


@router.patch("/studios/{studio_id}")
async def patch_studio(studio_id: int, body: StudioPatch, request: Request, acl: Acl = Depends(get_acl)):
    await entities.get_studio(studio_id, request, acl)
    await require_entity_edit(request.app.state.pool, acl, "studio", [studio_id])
    data = body.model_dump(exclude_unset=True)
    await require_visible(request.app.state.pool, acl, "studio", [data.get("parent_id")])
    if data.get("name", "x") is None or ("custom_fields" in data and data["custom_fields"] is None):
        raise HTTPException(422, "name/custom_fields cannot be null")
    await _write(request, lambda conn: _update(conn, "studios", studio_id, data))
    return await entities.get_studio(studio_id, request, acl)


class TagBody(Body):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    parent_ids: list[int] = []


class TagPatch(TagBody):
    name: str | None = Field(None, min_length=1, max_length=200)
    parent_ids: list[int] | None = None


async def _save_tag(conn, tag_id: int | None, data: dict, parents: list[int] | None) -> int:
    if tag_id is None:
        tag_id = await conn.fetchval(
            "INSERT INTO tags (name, description, owner_id) VALUES ($1, $2, $3) RETURNING id",
            data["name"], data.get("description"), data.get("owner_id"))
    elif not await _update(conn, "tags", tag_id, data):
        raise HTTPException(404, "tag not found")
    if parents is not None:
        await conn.execute("DELETE FROM tag_relations WHERE child_id = $1 AND parent_id <> ALL($2::bigint[])",
                           tag_id, parents)
        await conn.execute("INSERT INTO tag_relations SELECT unnest($2::bigint[]), $1 ON CONFLICT DO NOTHING",
                           tag_id, parents)
    return tag_id


@router.post("/tags", status_code=201)
async def create_tag(body: TagBody, request: Request, acl: Acl = Depends(get_acl)):
    owner = require_can_create(acl)
    await require_visible(request.app.state.pool, acl, "tag", body.parent_ids)
    tid = await _write(request, lambda conn: _save_tag(
        conn, None, body.model_dump(exclude={"parent_ids"}) | {"owner_id": owner}, body.parent_ids))
    return await entities.get_tag(tid, request, acl)


@router.patch("/tags/{tag_id}")
async def patch_tag(tag_id: int, body: TagPatch, request: Request, acl: Acl = Depends(get_acl)):
    await entities.get_tag(tag_id, request, acl)
    await require_entity_edit(request.app.state.pool, acl, "tag", [tag_id])
    data, links = _split(body, "parent_ids")
    await require_visible(request.app.state.pool, acl, "tag", links.get("parent_ids", []))
    if data.get("name", "x") is None:
        raise HTTPException(422, "name cannot be null")
    await _write(request, lambda conn: _save_tag(conn, tag_id, data, links.get("parent_ids")))
    return await entities.get_tag(tag_id, request, acl)


class TagMerge(Body):
    source_ids: list[int] = Field(min_length=1)


@router.post("/tags/{tag_id}/merge")
async def merge_tags(tag_id: int, body: TagMerge, request: Request, acl: Acl = Depends(get_acl)):
    """Fold source tags into this one: their content, performers, child tags and user
    rules move here, then the sources are deleted."""
    await entities.get_tag(tag_id, request, acl)
    sources = [s for s in dict.fromkeys(body.source_ids) if s != tag_id]
    pool = request.app.state.pool
    await require_visible(pool, acl, "tag", sources)   # unknown or invisible -> 422
    await require_entity_edit(pool, acl, "tag", [tag_id, *sources])

    async def fn(conn):
        found = await conn.fetchval("SELECT count(*) FROM tags WHERE id = ANY($1::bigint[])", sources)
        if found != len(sources):
            raise HTTPException(404, "some source tags do not exist")
        for table, fk in (("scene_tags", "scene_id"), ("gallery_tags", "gallery_id"),
                          ("image_tags", "image_id"), ("performer_tags", "performer_id"),
                          ("user_hidden_tags", "user_id")):
            await conn.execute(
                f"""INSERT INTO {table} ({fk}, tag_id) SELECT {fk}, $1 FROM {table}
                    WHERE tag_id = ANY($2::bigint[]) ON CONFLICT DO NOTHING""", tag_id, sources)
        await conn.execute(
            """INSERT INTO user_filter_tags (user_id, tag_id, show_only)
               SELECT DISTINCT ON (user_id) user_id, $1, show_only FROM user_filter_tags
               WHERE tag_id = ANY($2::bigint[]) ORDER BY user_id, show_only
               ON CONFLICT DO NOTHING""", tag_id, sources)
        # Children of the sources become children of the target, unless that would
        # create a cycle (a child that is also an ancestor of the target).
        await conn.execute(
            """INSERT INTO tag_relations (parent_id, child_id)
               SELECT DISTINCT $1::bigint, r.child_id FROM tag_relations r
               WHERE r.parent_id = ANY($2::bigint[]) AND r.child_id <> $1
                 AND r.child_id <> ALL($2::bigint[])
                 AND r.child_id NOT IN (
                     WITH RECURSIVE anc(id) AS (
                         SELECT parent_id FROM tag_relations WHERE child_id = $1
                         UNION SELECT tr.parent_id FROM tag_relations tr JOIN anc ON tr.child_id = anc.id)
                     SELECT id FROM anc)
               ON CONFLICT DO NOTHING""", tag_id, sources)
        await conn.execute("DELETE FROM tags WHERE id = ANY($1::bigint[])", sources)

    await _write(request, fn)
    return await entities.get_tag(tag_id, request, acl)


ENTITY_TABLES = {"performers": entities.get_performer, "studios": entities.get_studio, "tags": entities.get_tag}


async def delete_entity(kind: str, entity_id: int, request: Request, acl: Acl) -> Response:
    """Delete a performer/studio/tag. Content keeps existing; links to it are removed
    and child studios are detached."""
    await ENTITY_TABLES[kind](entity_id, request, acl)
    await require_entity_edit(request.app.state.pool, acl, kind[:-1], [entity_id])
    path = await _write(request, lambda conn: conn.fetchval(
        f"DELETE FROM {kind} WHERE id = $1 RETURNING image_path", entity_id))
    if path:
        (get_settings().generated_dir / path).unlink(missing_ok=True)
    _source_path(kind, entity_id).unlink(missing_ok=True)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Entity images (performer headshots, studio logos, tag images)
# ---------------------------------------------------------------------------

ENTITY_SOURCE_MAX = 2048   # the uncropped original kept for re-cropping


def _source_path(kind: str, entity_id: int):
    """The uncropped picture an entity's image was made from (kept so it can be re-cropped)."""
    return get_settings().generated_dir / f"entities/{kind}/{entity_id}.src.webp"


def _normalize_image(data: bytes, dest, max_size: int = ENTITY_IMAGE_MAX, crop=None) -> None:
    """Store `data` as WebP at `dest`, at most max_size px; `crop` = (x, y, w, h) as
    fractions of the picture."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            if crop:
                x, y, w, h = crop
                box = (round(x * im.width), round(y * im.height),
                       round((x + w) * im.width), round((y + h) * im.height))
                im = im.crop((max(0, box[0]), max(0, box[1]), min(im.width, box[2]), min(im.height, box[3])))
            im.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB")
            _atomic_write(dest, lambda f: im.save(f, "WEBP", quality=85, method=4))
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise HTTPException(422, f"not a usable image: {e}") from e


async def upload_entity_image(kind: str, entity_id: int, request: Request, acl: Acl) -> Response:
    """Raw image bytes in the body (any format Pillow reads); stored as WebP."""
    await ENTITY_TABLES[kind](entity_id, request, acl)
    await require_entity_edit(request.app.state.pool, acl, kind[:-1], [entity_id])
    size = int(request.headers.get("content-length") or 0)
    if size > MAX_IMAGE_UPLOAD:
        raise HTTPException(413, "image too large")
    data = await request.body()
    if not data or len(data) > MAX_IMAGE_UPLOAD:
        raise HTTPException(413 if data else 422, "image too large" if data else "empty body")
    return await _store_entity_image(request, kind, entity_id, data)


async def _store_entity_image(request: Request, kind: str, entity_id: int, data: bytes) -> Response:
    """Normalize to WebP and attach to the entity. The UPDATE also bumps updated_at,
    which is the image URL's version, so browsers pick up the new picture."""
    rel = f"entities/{kind}/{entity_id}.webp"
    await asyncio.to_thread(_normalize_image, data, _source_path(kind, entity_id), ENTITY_SOURCE_MAX)
    await asyncio.to_thread(_normalize_image, data, get_settings().generated_dir / rel)
    await request.app.state.pool.execute(f"UPDATE {kind} SET image_path = $2 WHERE id = $1", entity_id, rel)
    return Response(status_code=204)


class Crop(Body):
    # The part of the original picture to keep, as fractions of its width and height.
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)


async def crop_entity_image(kind: str, entity_id: int, body: Crop, request: Request, acl: Acl) -> Response:
    """Re-cut the entity's picture from its original (any number of times, no loss)."""
    await _editable_entity(kind, entity_id, request, acl)
    if body.x + body.w > 1.001 or body.y + body.h > 1.001:
        raise HTTPException(422, "the crop must lie inside the picture")
    rel = await request.app.state.pool.fetchval(f"SELECT image_path FROM {kind} WHERE id = $1", entity_id)
    if not rel:
        raise HTTPException(404, "no image to crop")
    src = _source_path(kind, entity_id)
    if not src.exists():                       # set before originals were kept: crop what there is
        src = get_settings().generated_dir / rel
    data = await asyncio.to_thread(src.read_bytes)
    await asyncio.to_thread(_normalize_image, data, get_settings().generated_dir / rel, ENTITY_IMAGE_MAX,
                            (body.x, body.y, min(body.w, 1 - body.x), min(body.h, 1 - body.y)))
    # touching the row bumps updated_at, which versions the image URL
    await request.app.state.pool.execute(f"UPDATE {kind} SET image_path = image_path WHERE id = $1", entity_id)
    return Response(status_code=204)


async def get_entity_image_source(kind: str, entity_id: int, request: Request, acl: Acl):
    """The uncropped original, for the crop tool."""
    await _editable_entity(kind, entity_id, request, acl)
    rel = await request.app.state.pool.fetchval(f"SELECT image_path FROM {kind} WHERE id = $1", entity_id)
    if not rel:
        raise HTTPException(404, "no image")
    src = _source_path(kind, entity_id)
    path = src if src.exists() else get_settings().generated_dir / rel
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, no-cache"})


async def _editable_entity(kind: str, entity_id: int, request: Request, acl: Acl) -> None:
    await ENTITY_TABLES[kind](entity_id, request, acl)   # 404 unless visible
    await require_entity_edit(request.app.state.pool, acl, kind[:-1], [entity_id])


class ImageFromUrl(Body):
    url: str = Field(min_length=1, max_length=2000)


async def entity_image_from_url(kind: str, entity_id: int, body: ImageFromUrl, request: Request, acl: Acl) -> Response:
    """Download an image from the internet and use it. See app/fetch.py for the rules."""
    await _editable_entity(kind, entity_id, request, acl)
    if not get_settings().url_image_fetch:
        raise HTTPException(403, "fetching images from URLs is disabled on this server")
    try:
        data = await fetch_image_bytes(body.url, max_bytes=MAX_IMAGE_UPLOAD)
    except FetchError as e:
        raise HTTPException(422, str(e)) from e
    return await _store_entity_image(request, kind, entity_id, data)


class ImageFromLibrary(Body):
    image_id: int | None = None
    scene_id: int | None = None


async def entity_image_from_library(kind: str, entity_id: int, body: ImageFromLibrary, request: Request,
                                    acl: Acl) -> Response:
    """Use an image that is already in the system: an indexed image, or a scene's
    screenshot. The source must be visible to whoever is doing this."""
    await _editable_entity(kind, entity_id, request, acl)
    if (body.image_id is None) == (body.scene_id is None):
        raise HTTPException(422, "give exactly one of image_id or scene_id")
    if body.image_id is not None:
        img = await media_api._image(request, body.image_id, acl)   # 404 if hidden or missing

        def read() -> bytes:
            with generated.open_image_source(img["path"], img["zip_path"]) as f:
                return f.read()
        data = await asyncio.to_thread(read)
    else:
        scene = await media_api._scene(request, body.scene_id, acl)
        version = generated.scene_version(scene["oshash"], scene["file_id"])
        shot = generated.screenshot_path(get_settings().generated_dir, version)
        if not generated.usable(shot):
            with crypto.private_outputs(scene["path"]):
                await generated.make_screenshot(scene["path"], scene["duration"], shot)
        data = await asyncio.to_thread(crypto.read_all, shot)
    return await _store_entity_image(request, kind, entity_id, data)


class RandomImage(Body):
    exclude_image_id: int | None = None   # the previous pick, so "again" gives a different one


# How content is linked to each kind of entity.
ASSIGNED = {"performers": "{p} = ANY(i.performer_ids)", "tags": "{p} = ANY(i.tag_ids)", "studios": "i.studio_id = {p}"}


async def entity_image_random(kind: str, entity_id: int, body: RandomImage, request: Request, acl: Acl) -> dict:
    """Use a random one of the images assigned to this performer (or tag, or studio), among
    those the caller can see. Returns which image was used."""
    await _editable_entity(kind, entity_id, request, acl)
    q = Q()
    where = [ASSIGNED[kind].format(p=q.p(entity_id)), browse.IMAGE_PRESENT, *visible(acl, "image", "i", q)]
    base = f"""SELECT i.id, i.assets_rev, f.id AS file_id, f.mod_time FROM images i
               JOIN files f ON f.id = i.file_id LEFT JOIN files z ON z.id = f.zip_file_id
               WHERE {' AND '.join(where)}"""
    pool = request.app.state.pool
    row = None
    if body.exclude_image_id is not None:
        row = await pool.fetchrow(f"{base} AND i.id <> {q.p(body.exclude_image_id)} ORDER BY random() LIMIT 1", *q.args)
        q.args.pop()
    if row is None:   # nothing else to choose from (or no previous pick)
        row = await pool.fetchrow(f"{base} ORDER BY random() LIMIT 1", *q.args)
    if row is None:
        raise HTTPException(404, f"no images are assigned to this {kind[:-1]} yet")
    await entity_image_from_library(kind, entity_id, ImageFromLibrary(image_id=row["id"]), request, acl)
    return {"image_id": row["id"],
            **browse.image_urls(row["id"], row["file_id"], row["mod_time"], 320, row["assets_rev"])}


class ImageFromPath(Body):
    path: str = Field(min_length=1, max_length=4000)


async def entity_image_from_path(kind: str, entity_id: int, body: ImageFromPath, request: Request, acl: Acl) -> Response:
    """Admins: use any image file in the read-only media store, indexed or not."""
    await _editable_entity(kind, entity_id, request, acl)
    path = media_file_path(body.path)
    if path.stat().st_size > MAX_SOURCE_IMAGE:
        raise HTTPException(413, "image too large")
    return await _store_entity_image(request, kind, entity_id, await asyncio.to_thread(path.read_bytes))


async def delete_entity_image(kind: str, entity_id: int, request: Request, acl: Acl) -> Response:
    await ENTITY_TABLES[kind](entity_id, request, acl)
    await require_entity_edit(request.app.state.pool, acl, kind[:-1], [entity_id])
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        path = await conn.fetchval(f"SELECT image_path FROM {kind} WHERE id = $1 FOR UPDATE", entity_id)
        await conn.execute(f"UPDATE {kind} SET image_path = NULL WHERE id = $1", entity_id)
    if path:
        (get_settings().generated_dir / path).unlink(missing_ok=True)
    _source_path(kind, entity_id).unlink(missing_ok=True)
    return Response(status_code=204)


async def get_entity_image(kind: str, entity_id: int, request: Request, v: str | None, acl: Acl):
    item = await ENTITY_TABLES[kind](entity_id, request, acl)
    if not item.get("image_url"):
        raise HTTPException(404, "no image")
    version = item["image_url"].rsplit("v=", 1)[1]
    path = get_settings().generated_dir / f"entities/{kind}/{entity_id}.webp"
    if not path.exists():
        raise HTTPException(404, "no image")
    return FileResponse(path, media_type="image/webp", headers={
        "Cache-Control": "private, max-age=31536000, immutable" if v == version else "private, no-cache"})


def _entity_routes(kind: str) -> None:
    async def delete(entity_id: int, request: Request, acl: Acl = Depends(get_acl)) -> Response:
        return await delete_entity(kind, entity_id, request, acl)

    async def put_image(entity_id: int, request: Request, acl: Acl = Depends(get_acl)) -> Response:
        return await upload_entity_image(kind, entity_id, request, acl)

    async def delete_image(entity_id: int, request: Request, acl: Acl = Depends(get_acl)) -> Response:
        return await delete_entity_image(kind, entity_id, request, acl)

    async def get_image(entity_id: int, request: Request, v: str | None = None, acl: Acl = Depends(get_acl)):
        return await get_entity_image(kind, entity_id, request, v, acl)

    async def from_url(entity_id: int, body: ImageFromUrl, request: Request, acl: Acl = Depends(get_acl)) -> Response:
        return await entity_image_from_url(kind, entity_id, body, request, acl)

    async def from_library(entity_id: int, body: ImageFromLibrary, request: Request,
                           acl: Acl = Depends(get_acl)) -> Response:
        return await entity_image_from_library(kind, entity_id, body, request, acl)

    async def random_image(entity_id: int, request: Request, body: RandomImage | None = None,
                           acl: Acl = Depends(get_acl)) -> dict:
        return await entity_image_random(kind, entity_id, body or RandomImage(), request, acl)

    async def from_path(entity_id: int, body: ImageFromPath, request: Request, acl: Acl = Depends(get_acl),
                        _admin=Depends(require_admin)) -> Response:
        return await entity_image_from_path(kind, entity_id, body, request, acl)

    async def crop(entity_id: int, body: Crop, request: Request, acl: Acl = Depends(get_acl)) -> Response:
        return await crop_entity_image(kind, entity_id, body, request, acl)

    async def source(entity_id: int, request: Request, acl: Acl = Depends(get_acl)):
        return await get_entity_image_source(kind, entity_id, request, acl)

    for fn, method, path, code in ((delete, "DELETE", "", 204), (put_image, "PUT", "/image", 204),
                                   (crop, "POST", "/image/crop", 204), (source, "GET", "/image/source", None),
                                   (delete_image, "DELETE", "/image", 204), (get_image, "GET", "/image", None),
                                   (from_url, "POST", "/image/from-url", 204),
                                   (from_library, "POST", "/image/from-library", 204),
                                   (random_image, "POST", "/image/random", 200),
                                   (from_path, "POST", "/image/from-path", 204)):
        fn.__name__ = f"{fn.__name__}_{kind}"
        target = read_router if method == "GET" else router
        target.add_api_route(f"/{kind}/{{entity_id}}{path}", fn, methods=[method],
                             **({"status_code": code} if code else {}))


for _k in ENTITY_TABLES:
    _entity_routes(_k)
