"""Read API for content: scenes, galleries, images.

List payloads are deliberately small (ids, short strings, thumbnail URLs, and
{id, name} pairs for studio/performers/tags aggregated in the same query); heavier
fields are only returned by the detail routes.
"""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app import generated
from app.acl import Acl, visible
from app.auth import get_acl
from app.permissions import may_edit
from app.sql import ListQuery, Q, Sort, paginate

router = APIRouter(tags=["browse"])

LINKS = {
    "scene": ("scene_tags", "scene_performers", "scene_id"),
    "gallery": ("gallery_tags", "gallery_performers", "gallery_id"),
    "image": ("image_tags", "image_performers", "image_id"),
}
ThumbWidth = Literal[160, 320, 640, 1280]


# ---------------------------------------------------------------------------
# Shared filtering
# ---------------------------------------------------------------------------

class ListParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(60, ge=1, le=200)
    cursor: str | None = None
    desc: bool = True
    with_total: bool = False
    thumb_w: int = 640


class ContentFilter(ListParams):
    q: str | None = Field(None, max_length=200, description="title contains")
    tag_ids: list[int] = []
    tag_mode: Literal["all", "any"] = "all"
    exclude_tag_ids: list[int] = []
    include_sub_tags: bool = True
    performer_ids: list[int] = []
    performer_mode: Literal["all", "any"] = "all"
    studio_ids: list[int] = []
    include_sub_studios: bool = True
    rating_min: int | None = Field(None, ge=0, le=100)
    organized: bool | None = None
    library_ids: list[int] = []
    # internal: tag_ids resolved into groups by expand_hierarchies()
    tag_groups: list[list[int]] | None = Field(None, exclude=True)


def like(q: Q, text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return q.p(f"%{escaped}%")


def tag_set(q: Q, ids: list[int], include_sub: bool) -> str:
    ph = q.p(ids)
    return f"ARRAY(SELECT tag_descendants({ph}::bigint[]))" if include_sub else f"{ph}::bigint[]"


async def expand_hierarchies(pool, f: "ContentFilter") -> "ContentFilter":
    """Resolve sub-tag / sub-studio trees to literal id lists up front (one query).

    Passed as plain array parameters, the planner can use column statistics to see
    how selective each set is; computed inside the query it has to guess, and a
    wrong guess on a broad set costs 100x (measured: 651 ms -> single digits)."""
    groups = [f.tag_ids] if f.tag_mode == "any" else [[t] for t in f.tag_ids]
    wants_tags = f.include_sub_tags and (groups or f.exclude_tag_ids)
    wants_studios = f.include_sub_studios and f.studio_ids
    if not (wants_tags or wants_studios):
        return f.model_copy(update={"tag_groups": groups})
    row = await pool.fetchrow(
        """SELECT ARRAY(SELECT to_jsonb(ARRAY(SELECT tag_descendants(x.ids)))
                        FROM (SELECT grp, array_agg(id) AS ids
                              FROM unnest($4::bigint[], $1::int[]) u(id, grp) GROUP BY grp) x
                        ORDER BY x.grp) AS groups,
                  ARRAY(SELECT tag_descendants($2::bigint[])) AS excluded,
                  ARRAY(SELECT studio_descendants($3::bigint[])) AS studios""",
        [n for n, g in enumerate(groups) for _ in g] if wants_tags else [],
        f.exclude_tag_ids if wants_tags else [],
        f.studio_ids if wants_studios else [],
        [t for g in groups for t in g] if wants_tags else [],
    )
    update = {"tag_groups": groups}
    if wants_tags:
        update |= {"tag_groups": list(row["groups"]), "exclude_tag_ids": list(row["excluded"]),
                   "include_sub_tags": False}
    if wants_studios:
        update |= {"studio_ids": list(row["studios"]), "include_sub_studios": False}
    return f.model_copy(update=update)


def content_where(q: Q, f: ContentFilter, kind: str, alias: str, acl: Acl) -> list[str]:
    """Filters on the trigger-maintained tag_ids / performer_ids arrays (GIN-indexed)."""
    w = visible(acl, kind, alias, q)
    if f.q:
        w.append(f"{alias}.title ILIKE {like(q, f.q)}")
    groups = f.tag_groups if f.tag_groups is not None else (
        [f.tag_ids] if f.tag_mode == "any" else [[t] for t in f.tag_ids])
    w += [f"{alias}.tag_ids && {tag_set(q, g, f.include_sub_tags)}" for g in groups]
    if f.exclude_tag_ids:
        w.append(f"NOT ({alias}.tag_ids && {tag_set(q, f.exclude_tag_ids, f.include_sub_tags)})")
    if f.performer_ids:
        op = "&&" if f.performer_mode == "any" else "@>"
        w.append(f"{alias}.performer_ids {op} {q.p(f.performer_ids)}::bigint[]")
    if f.studio_ids:
        ph = q.p(f.studio_ids)
        ids = f"ARRAY(SELECT studio_descendants({ph}::bigint[]))" if f.include_sub_studios else f"{ph}::bigint[]"
        w.append(f"{alias}.studio_id = ANY({ids})")
    if f.rating_min is not None:
        w.append(f"coalesce({alias}.rating, -1) >= {q.p(f.rating_min)}")
    if f.organized is not None:
        w.append(f"{alias}.organized = {q.p(f.organized)}")
    if f.library_ids:
        w.append(f"{alias}.library_id = ANY({q.p(f.library_ids)}::bigint[])")
    return w


def related(kind: str) -> tuple[str, str]:
    """LATERAL joins adding {id, name} studio/performers/tags to page rows `p`."""
    tag_table, perf_table, fk = LINKS[kind]
    joins = f"""
        LEFT JOIN LATERAL (
            SELECT jsonb_build_object('id', x.id, 'name', x.name) AS studio
            FROM studios x WHERE x.id = p.studio_id) r_st ON true
        LEFT JOIN LATERAL (
            SELECT coalesce(jsonb_agg(jsonb_build_object('id', x.id, 'name', x.name)
                                      ORDER BY x.name COLLATE natural_sort), '[]') AS performers
            FROM {perf_table} l JOIN performers x ON x.id = l.performer_id WHERE l.{fk} = p.id) r_pf ON true
        LEFT JOIN LATERAL (
            SELECT coalesce(jsonb_agg(jsonb_build_object('id', x.id, 'name', x.name)
                                      ORDER BY x.name COLLATE natural_sort), '[]') AS tags
            FROM {tag_table} l JOIN tags x ON x.id = l.tag_id WHERE l.{fk} = p.id) r_tg ON true"""
    return joins, ", r_st.studio, r_pf.performers, r_tg.tags"


def page_response(page) -> dict:
    return {"items": page.items, "next_cursor": page.next_cursor, "total": page.total}


def scene_thumb(item: dict, w: int) -> str:
    v = generated.scene_version(item["oshash"], item["file_id"])
    return f"/api/scenes/{item['id']}/screenshot?w={w}&v={v}"


def image_urls(image_id: int, file_id: int, mod_time, w: int) -> dict:
    v = generated.image_version(file_id, mod_time)
    return {"thumbnail_url": f"/api/images/{image_id}/thumbnail?w={w}&v={v}",
            "file_url": f"/api/images/{image_id}/file?v={v}"}


def _edit_flags(out: dict, acl: Acl) -> None:
    """Replace the internal library owner with what the client needs: may I edit this,
    and is it from my own library."""
    owner = out.pop("library_owner_id")
    out["can_edit"] = may_edit(acl, owner)
    out["mine"] = owner is not None and owner == acl.user_id
    for internal in ("tag_ids", "performer_ids"):
        out.pop(internal, None)
    if not out["can_edit"]:
        # Where a file lives on disk is only the business of whoever manages it.
        if isinstance(out.get("file"), dict):
            out["file"] = {k: v for k, v in out["file"].items() if k not in ("path", "zip_path")}
        for k in ("folder_path", "zip_path"):
            if k in out:
                out[k] = None


def _check_thumb(w: int) -> None:
    if w not in generated.THUMB_WIDTHS:
        raise HTTPException(422, f"thumb_w must be one of {generated.THUMB_WIDTHS}")


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------

SCENE_SORTS = {
    "created_at": Sort("s.created_at", "timestamptz"),
    "updated_at": Sort("s.updated_at", "timestamptz"),
    "release_date": Sort("coalesce(s.release_date, '0001-01-01'::date)", "date"),
    "title": Sort("coalesce(s.title, '')", "text", collate="natural_sort"),
    "duration": Sort("coalesce(s.duration, -1)", "real"),
    "resolution": Sort("coalesce(s.height, 0)", "integer"),
    "rating": Sort("coalesce(s.rating, -1)", "smallint"),
    "play_count": Sort("s.play_count", "integer"),
    "last_played_at": Sort("coalesce(s.last_played_at, '0001-01-01 00:00+00'::timestamptz)", "timestamptz"),
}


class SceneFilter(ContentFilter):
    sort: str = "created_at"
    duration_min: float | None = Field(None, ge=0)
    duration_max: float | None = Field(None, ge=0)
    height_min: int | None = Field(None, ge=0, description="minimum vertical resolution")
    date_from: date | None = None
    date_to: date | None = None
    played: bool | None = None
    file_status: Literal["present", "missing", "all"] = "present"


def scene_where(q: Q, f: SceneFilter, acl: Acl) -> list[str]:
    w = content_where(q, f, "scene", "s", acl)
    if f.file_status == "present":
        w.append("f.missing_since IS NULL")
    elif f.file_status == "missing":
        w.append("f.missing_since IS NOT NULL")
    if f.duration_min is not None:
        w.append(f"s.duration >= {q.p(f.duration_min)}")
    if f.duration_max is not None:
        w.append(f"s.duration <= {q.p(f.duration_max)}")
    if f.height_min is not None:
        w.append(f"s.height >= {q.p(f.height_min)}")
    if f.date_from is not None:
        w.append(f"s.release_date >= {q.p(f.date_from)}")
    if f.date_to is not None:
        w.append(f"s.release_date <= {q.p(f.date_to)}")
    if f.played is not None:
        w.append("s.play_count > 0" if f.played else "s.play_count = 0")
    return w


def _scene_enrich(q: Q, acl: Acl) -> tuple[str, str]:
    joins, cols = related("scene")
    joins += f"""
        LEFT JOIN user_scene_state r_us ON r_us.user_id = {q.p(acl.user_id, 'viewer_id')}::bigint
                                        AND r_us.scene_id = p.id"""
    return joins, cols + ", r_us.resume_time"


@router.get("/scenes")
async def list_scenes(request: Request, f: Annotated[SceneFilter, Query()],
                      acl: Acl = Depends(get_acl)) -> dict:
    _check_thumb(f.thumb_w)
    f = await expand_hierarchies(request.app.state.pool, f)
    lq = ListQuery(
        alias="s",
        from_="scenes s JOIN files f ON f.id = s.file_id",
        select="""s.id, s.title, s.duration, s.width, s.height, s.release_date, s.rating,
                  s.organized, s.play_count, s.studio_id, s.created_at,
                  f.id AS file_id, f.oshash, f.missing_since IS NOT NULL AS missing""",
        sorts=SCENE_SORTS,
        where=lambda q: scene_where(q, f, acl),
        enrich=lambda q: _scene_enrich(q, acl),
    )
    page = await paginate(request.app.state.pool, lq, sort=f.sort, desc=f.desc,
                          cursor=f.cursor, limit=f.limit, with_total=f.with_total)
    for it in page.items:
        it["screenshot_url"] = scene_thumb(it, f.thumb_w)
        for k in ("file_id", "oshash", "studio_id"):
            del it[k]
    return page_response(page)


@router.get("/scenes/{scene_id}")
async def get_scene(scene_id: int, request: Request, acl: Acl = Depends(get_acl)) -> dict:
    q = Q()
    q.p(scene_id)
    cond = " AND ".join(["s.id = $1", *visible(acl, "scene", "s", q)])
    row = await request.app.state.pool.fetchrow(f"""
        SELECT s.*, f.oshash,
               (SELECT l.owner_id FROM library_paths l WHERE l.id = s.library_id) AS library_owner_id,
               (SELECT resume_time FROM user_scene_state
                WHERE user_id = {q.p(acl.user_id)}::bigint AND scene_id = s.id) AS resume_time,
               jsonb_build_object('path', f.path, 'size', f.size, 'mod_time', f.mod_time,
                                  'oshash', f.oshash, 'missing_since', f.missing_since) AS file,
               (SELECT jsonb_build_object('id', x.id, 'name', x.name, 'parent_id', x.parent_id)
                FROM studios x WHERE x.id = s.studio_id) AS studio,
               (SELECT coalesce(jsonb_agg(jsonb_build_object(
                           'id', x.id, 'name', x.name, 'disambiguation', x.disambiguation,
                           'gender', x.gender, 'has_image', x.image_path IS NOT NULL,
                           'image_version', floor(extract(epoch FROM x.updated_at))::bigint)
                           ORDER BY x.name COLLATE natural_sort), '[]')
                FROM scene_performers l JOIN performers x ON x.id = l.performer_id
                WHERE l.scene_id = s.id) AS performers,
               (SELECT coalesce(jsonb_agg(jsonb_build_object('id', x.id, 'name', x.name)
                                          ORDER BY x.name COLLATE natural_sort), '[]')
                FROM scene_tags l JOIN tags x ON x.id = l.tag_id WHERE l.scene_id = s.id) AS tags
        FROM scenes s JOIN files f ON f.id = s.file_id
        WHERE {cond}""", *q.args)
    if row is None:
        raise HTTPException(404, "scene not found")
    out = dict(row)
    _edit_flags(out, acl)
    out["screenshot_url"] = scene_thumb(out, 1280)
    out["playback_url"] = f"/api/scenes/{scene_id}/playback"
    for p in out["performers"]:
        version = p.pop("image_version")
        p["image_url"] = f"/api/performers/{p['id']}/image?v={version}" if p.pop("has_image") else None
    for k in ("file_id", "oshash", "studio_id"):
        del out[k]
    return out


# ---------------------------------------------------------------------------
# Galleries
# ---------------------------------------------------------------------------

GALLERY_SORTS = {
    "created_at": Sort("g.created_at", "timestamptz"),
    "release_date": Sort("coalesce(g.release_date, '0001-01-01'::date)", "date"),
    "title": Sort("coalesce(g.title, '')", "text", collate="natural_sort"),
    "rating": Sort("coalesce(g.rating, -1)", "smallint"),
    "image_count": Sort("g.image_count", "integer"),
}


class GalleryFilter(ContentFilter):
    sort: str = "created_at"
    kind: Literal["folder", "zip", "virtual", "all"] = "all"


@router.get("/galleries")
async def list_galleries(request: Request, f: Annotated[GalleryFilter, Query()],
                         acl: Acl = Depends(get_acl)) -> dict:
    _check_thumb(f.thumb_w)
    f = await expand_hierarchies(request.app.state.pool, f)

    def where(q: Q) -> list[str]:
        w = content_where(q, f, "gallery", "g", acl)
        w.append("g.image_count > 0")
        if f.kind == "folder":
            w.append("g.folder_path IS NOT NULL")
        elif f.kind == "zip":
            w.append("g.zip_file_id IS NOT NULL")
        elif f.kind == "virtual":
            w.append("g.folder_path IS NULL AND g.zip_file_id IS NULL")
        return w

    def enrich(q: Q):
        joins, cols = related("gallery")
        joins += """
            LEFT JOIN LATERAL (
                SELECT cf.id AS cover_file_id, cf.mod_time AS cover_mod_time
                FROM images ci JOIN files cf ON cf.id = ci.file_id WHERE ci.id = p.cover_image_id) r_cv ON true"""
        return joins, cols + ", r_cv.cover_file_id, r_cv.cover_mod_time"

    lq = ListQuery(
        alias="g", from_="galleries g",
        select="""g.id, g.title, g.release_date, g.rating, g.organized, g.image_count,
                  g.studio_id, g.cover_image_id, g.created_at""",
        sorts=GALLERY_SORTS, where=where, enrich=enrich,
    )
    page = await paginate(request.app.state.pool, lq, sort=f.sort, desc=f.desc,
                          cursor=f.cursor, limit=f.limit, with_total=f.with_total)
    for it in page.items:
        fid, mt = it.pop("cover_file_id"), it.pop("cover_mod_time")
        it["cover_url"] = image_urls(it["cover_image_id"], fid, mt, f.thumb_w)["thumbnail_url"] if fid else None
        del it["studio_id"]
    return page_response(page)


@router.get("/galleries/{gallery_id}")
async def get_gallery(gallery_id: int, request: Request, acl: Acl = Depends(get_acl)) -> dict:
    q = Q()
    q.p(gallery_id)
    cond = " AND ".join(["g.id = $1", *visible(acl, "gallery", "g", q)])
    row = await request.app.state.pool.fetchrow(f"""
        SELECT g.*, z.path AS zip_path,
               (SELECT l.owner_id FROM library_paths l WHERE l.id = g.library_id) AS library_owner_id,
               cf.id AS cover_file_id, cf.mod_time AS cover_mod_time,
               (SELECT jsonb_build_object('id', x.id, 'name', x.name) FROM studios x WHERE x.id = g.studio_id) AS studio,
               (SELECT coalesce(jsonb_agg(jsonb_build_object('id', x.id, 'name', x.name)
                                          ORDER BY x.name COLLATE natural_sort), '[]')
                FROM gallery_performers l JOIN performers x ON x.id = l.performer_id
                WHERE l.gallery_id = g.id) AS performers,
               (SELECT coalesce(jsonb_agg(jsonb_build_object('id', x.id, 'name', x.name)
                                          ORDER BY x.name COLLATE natural_sort), '[]')
                FROM gallery_tags l JOIN tags x ON x.id = l.tag_id WHERE l.gallery_id = g.id) AS tags
        FROM galleries g
        LEFT JOIN files z ON z.id = g.zip_file_id
        LEFT JOIN images ci ON ci.id = g.cover_image_id
        LEFT JOIN files cf ON cf.id = ci.file_id
        WHERE {cond}""", *q.args)
    if row is None:
        raise HTTPException(404, "gallery not found")
    out = dict(row)
    _edit_flags(out, acl)
    fid, mt = out.pop("cover_file_id"), out.pop("cover_mod_time")
    out["cover_url"] = image_urls(out["cover_image_id"], fid, mt, 640)["thumbnail_url"] if fid else None
    out["images_url"] = f"/api/galleries/{gallery_id}/images"
    del out["zip_file_id"], out["studio_id"]
    return out


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

IMAGE_SORTS = {
    "created_at": Sort("i.created_at", "timestamptz"),
    "title": Sort("coalesce(i.title, '')", "text", collate="natural_sort"),
    "rating": Sort("coalesce(i.rating, -1)", "smallint"),
}
IMAGE_SELECT = """i.id, i.title, i.width, i.height, i.rating, i.organized,
                  f.id AS file_id, f.mod_time"""
IMAGE_JOINS = "JOIN files f ON f.id = i.file_id LEFT JOIN files z ON z.id = f.zip_file_id"
IMAGE_FROM = f"images i {IMAGE_JOINS}"
IMAGE_PRESENT = "f.missing_since IS NULL AND z.missing_since IS NULL"


class ImageFilter(ContentFilter):
    sort: str = "created_at"
    thumb_w: int = 320


def _image_items(page, w: int) -> dict:
    for it in page.items:
        it.update(image_urls(it["id"], it.pop("file_id"), it.pop("mod_time"), w))
    return page_response(page)


@router.get("/images")
async def list_images(request: Request, f: Annotated[ImageFilter, Query()],
                      acl: Acl = Depends(get_acl)) -> dict:
    _check_thumb(f.thumb_w)
    f = await expand_hierarchies(request.app.state.pool, f)
    lq = ListQuery(
        alias="i", from_=IMAGE_FROM, select=IMAGE_SELECT, sorts=IMAGE_SORTS,
        where=lambda q: [IMAGE_PRESENT, *content_where(q, f, "image", "i", acl)],
    )
    page = await paginate(request.app.state.pool, lq, sort=f.sort, desc=f.desc,
                          cursor=f.cursor, limit=f.limit, with_total=f.with_total)
    return _image_items(page, f.thumb_w)


class GalleryImagesParams(ListParams):
    desc: bool = False
    thumb_w: int = 320


@router.get("/galleries/{gallery_id}/images")
async def gallery_images(gallery_id: int, request: Request, f: Annotated[GalleryImagesParams, Query()],
                         acl: Acl = Depends(get_acl)) -> dict:
    """Images in gallery order. Width/height are included so the lightbox and justified
    grids can lay out before any image loads."""
    _check_thumb(f.thumb_w)
    pool = request.app.state.pool
    gq = Q()
    gq.p(gallery_id)
    gcond = " AND ".join(["g.id = $1", *visible(acl, "gallery", "g", gq)])
    if not await pool.fetchval(f"SELECT true FROM galleries g WHERE {gcond}", *gq.args):
        raise HTTPException(404, "gallery not found")

    def where(q: Q) -> list[str]:
        return [f"gi.gallery_id = {q.p(gallery_id)}", IMAGE_PRESENT, *visible(acl, "image", "i", q)]

    lq = ListQuery(
        alias="i", from_=f"gallery_images gi JOIN images i ON i.id = gi.image_id {IMAGE_JOINS}", select=IMAGE_SELECT + ", gi.position",
        sorts={"position": Sort("gi.position", "integer")}, where=where,
    )
    page = await paginate(pool, lq, sort="position", desc=f.desc, cursor=f.cursor,
                          limit=f.limit, with_total=f.with_total)
    return _image_items(page, f.thumb_w)


@router.get("/images/{image_id}")
async def get_image(image_id: int, request: Request, acl: Acl = Depends(get_acl)) -> dict:
    q = Q()
    q.p(image_id)
    cond = " AND ".join(["i.id = $1", *visible(acl, "image", "i", q)])
    row = await request.app.state.pool.fetchrow(f"""
        SELECT i.*, f.mod_time,
               (SELECT l.owner_id FROM library_paths l WHERE l.id = i.library_id) AS library_owner_id,
               jsonb_build_object('path', f.path, 'zip_path', z.path, 'size', f.size,
                                  'missing', f.missing_since IS NOT NULL OR z.missing_since IS NOT NULL) AS file,
               (SELECT jsonb_build_object('id', x.id, 'name', x.name) FROM studios x WHERE x.id = i.studio_id) AS studio,
               (SELECT coalesce(jsonb_agg(jsonb_build_object('id', g.id, 'title', g.title) ORDER BY g.id), '[]')
                FROM gallery_images gi JOIN galleries g ON g.id = gi.gallery_id WHERE gi.image_id = i.id) AS galleries,
               (SELECT coalesce(jsonb_agg(jsonb_build_object('id', x.id, 'name', x.name)
                                          ORDER BY x.name COLLATE natural_sort), '[]')
                FROM image_performers l JOIN performers x ON x.id = l.performer_id
                WHERE l.image_id = i.id) AS performers,
               (SELECT coalesce(jsonb_agg(jsonb_build_object('id', x.id, 'name', x.name)
                                          ORDER BY x.name COLLATE natural_sort), '[]')
                FROM image_tags l JOIN tags x ON x.id = l.tag_id WHERE l.image_id = i.id) AS tags
        FROM {IMAGE_FROM}
        WHERE {cond}""", *q.args)
    if row is None:
        raise HTTPException(404, "image not found")
    out = dict(row)
    _edit_flags(out, acl)
    out.update(image_urls(image_id, out.pop("file_id"), out.pop("mod_time"), 1280))
    del out["studio_id"]
    return out
