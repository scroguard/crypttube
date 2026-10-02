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
    limit: int = Field(60, ge=1, le=1000)
    cursor: str | None = None
    # Jump straight to a page (rows to skip from the start). Scrolling uses the cursor,
    # which stays fast at any depth; an offset costs a scan of the rows it skips.
    offset: int = Field(0, ge=0, le=50_000_000)
    desc: bool = True
    with_total: bool = False
    thumb_w: int = 640


class ContentFilter(ListParams):
    q: str | None = Field(None, max_length=200, description="title or file path contains")
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
    no_performers: bool | None = Field(None, description="true: only items with no performer assigned")
    mine: bool | None = Field(None, description="true: only items in my own libraries")
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


# Ids whose title or file path contains the text. Written as a UNION of single-column
# searches so each one can use its trigram index (an OR across the joined tables would
# scan). Paths: a scene's/image's file, an image's zip archive, a gallery's folder or zip.
_SEARCH_SOURCES = {
    "scene": """SELECT id FROM scenes WHERE title ILIKE {p}
                UNION ALL SELECT x.id FROM files pf JOIN scenes x ON x.file_id = pf.id WHERE pf.path ILIKE {p}""",
    "image": """SELECT id FROM images WHERE title ILIKE {p}
                UNION ALL SELECT x.id FROM files pf JOIN images x ON x.file_id = pf.id WHERE pf.path ILIKE {p}
                UNION ALL SELECT x.id FROM files zf JOIN files m ON m.zip_file_id = zf.id
                          JOIN images x ON x.file_id = m.id WHERE zf.path ILIKE {p}""",
    "gallery": """SELECT id FROM galleries WHERE title ILIKE {p} OR folder_path ILIKE {p}
                  UNION ALL SELECT x.id FROM files zf JOIN galleries x ON x.zip_file_id = zf.id WHERE zf.path ILIKE {p}""",
}


def text_search(q: Q, kind: str, alias: str, text: str) -> str:
    return f"{alias}.id IN ({_SEARCH_SOURCES[kind].format(p=like(q, text))})"


def content_where(q: Q, f: ContentFilter, kind: str, alias: str, acl: Acl) -> list[str]:
    """Filters on the trigger-maintained tag_ids / performer_ids arrays (GIN-indexed)."""
    w = visible(acl, kind, alias, q)
    if f.q:
        w.append(text_search(q, kind, alias, f.q))
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
    if f.no_performers is not None:
        w.append(f"cardinality({alias}.performer_ids) {'=' if f.no_performers else '>'} 0")
    if f.mine:
        w.append(f"{alias}.library_id IN (SELECT id FROM library_paths "
                 f"WHERE owner_id = {q.p(acl.user_id, 'acl_me')}::bigint)")
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
    v = generated.url_version(generated.scene_version(item["oshash"], item["file_id"]), item["assets_rev"])
    return f"/api/scenes/{item['id']}/screenshot?w={w}&v={v}"


def image_urls(image_id: int, file_id: int, mod_time, w: int, rev: int = 0) -> dict:
    v = generated.url_version(generated.image_version(file_id, mod_time), rev)
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


SCENE_FROM = "scenes s JOIN files f ON f.id = s.file_id"
SCENE_SELECT = """s.id, s.title, s.duration, s.width, s.height, s.release_date, s.rating,
                  s.organized, s.play_count, s.studio_id, s.created_at,
                  s.assets_rev, f.id AS file_id, f.oshash, f.missing_since IS NOT NULL AS missing"""


def finish_scene(it: dict, thumb_w: int) -> dict:
    """Turn a SCENE_SELECT (+ enrich) row into the list item clients get."""
    it["screenshot_url"] = scene_thumb(it, thumb_w)
    for k in ("file_id", "oshash", "studio_id", "assets_rev"):
        del it[k]
    return it


async def scenes_by_id(pool, ids: list[int], acl: Acl, thumb_w: int) -> dict[int, dict]:
    """List-shaped scene items for specific ids (callers have already applied visibility)."""
    q = Q()
    ph = q.p(ids)
    joins, cols = _scene_enrich(q, acl)
    rows = await pool.fetch(
        f"""WITH p AS (SELECT {SCENE_SELECT} FROM {SCENE_FROM} WHERE s.id = ANY({ph}::bigint[]))
            SELECT p.* {cols} FROM p {joins}""", *q.args)
    return {r["id"]: finish_scene(dict(r), thumb_w) for r in rows}


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
        from_=SCENE_FROM,
        select=SCENE_SELECT,
        sorts=SCENE_SORTS,
        where=lambda q: scene_where(q, f, acl),
        enrich=lambda q: _scene_enrich(q, acl),
    )
    page = await paginate(request.app.state.pool, lq, sort=f.sort, desc=f.desc,
                          cursor=f.cursor, offset=f.offset, limit=f.limit, with_total=f.with_total)
    for it in page.items:
        finish_scene(it, f.thumb_w)
    return page_response(page)


SELECT_ALL_LIMIT = 20_000


def editable_clause(acl: Acl, alias: str, q: Q) -> str:
    """Rows this viewer may edit: global-library content for editors, their own always."""
    me = q.p(acl.user_id, "acl_me")
    owned = f"SELECT id FROM library_paths WHERE owner_id = {me}::bigint"
    if acl.editor:
        return (f"({alias}.library_id IS NULL OR {alias}.library_id IN "
                f"(SELECT id FROM library_paths WHERE owner_id IS NULL OR owner_id = {me}::bigint))")
    return f"{alias}.library_id IN ({owned})"


async def _matching_ids(pool, from_: str, alias: str, where: list[str], args: list) -> dict:
    rows = await pool.fetch(
        f"SELECT {alias}.id FROM {from_} WHERE {' AND '.join(where)} ORDER BY {alias}.id LIMIT {SELECT_ALL_LIMIT + 1}",
        *args)
    ids = [r["id"] for r in rows[:SELECT_ALL_LIMIT]]
    return {"ids": ids, "truncated": len(rows) > SELECT_ALL_LIMIT}


@router.get("/ids/scenes")
async def scene_ids_matching(request: Request, f: Annotated[SceneFilter, Query()],
                             acl: Acl = Depends(get_acl)) -> dict:
    """Ids of every scene that matches the filters and that the viewer may edit: what
    "select all" selects, including items not loaded on screen."""
    pool = request.app.state.pool
    f = await expand_hierarchies(pool, f)
    q = Q()
    where = [*scene_where(q, f, acl), editable_clause(acl, "s", q)]
    return await _matching_ids(pool, SCENE_FROM, "s", where, q.args)


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
                           'image_version', floor(extract(epoch FROM x.updated_at) * 1000)::bigint)
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
    del out["assets_rev"]
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
                SELECT cf.id AS cover_file_id, cf.mod_time AS cover_mod_time, ci.assets_rev AS cover_rev
                FROM images ci JOIN files cf ON cf.id = ci.file_id WHERE ci.id = p.cover_image_id) r_cv ON true"""
        return joins, cols + ", r_cv.cover_file_id, r_cv.cover_mod_time, r_cv.cover_rev"

    lq = ListQuery(
        alias="g", from_="galleries g",
        select="""g.id, g.title, g.release_date, g.rating, g.organized, g.image_count,
                  g.studio_id, g.cover_image_id, g.created_at""",
        sorts=GALLERY_SORTS, where=where, enrich=enrich,
    )
    page = await paginate(request.app.state.pool, lq, sort=f.sort, desc=f.desc,
                          cursor=f.cursor, offset=f.offset, limit=f.limit, with_total=f.with_total)
    for it in page.items:
        fid, mt, rev = it.pop("cover_file_id"), it.pop("cover_mod_time"), it.pop("cover_rev")
        it["cover_url"] = image_urls(it["cover_image_id"], fid, mt, f.thumb_w, rev)["thumbnail_url"] if fid else None
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
               cf.id AS cover_file_id, cf.mod_time AS cover_mod_time, ci.assets_rev AS cover_rev,
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
    fid, mt, rev = out.pop("cover_file_id"), out.pop("cover_mod_time"), out.pop("cover_rev")
    out["cover_url"] = image_urls(out["cover_image_id"], fid, mt, 640, rev)["thumbnail_url"] if fid else None
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
IMAGE_SELECT = """i.id, i.title, i.width, i.height, i.rating, i.organized, i.assets_rev,
                  f.id AS file_id, f.mod_time"""
IMAGE_JOINS = "JOIN files f ON f.id = i.file_id LEFT JOIN files z ON z.id = f.zip_file_id"
IMAGE_FROM = f"images i {IMAGE_JOINS}"
IMAGE_PRESENT = "f.missing_since IS NULL AND z.missing_since IS NULL"


class ImageFilter(ContentFilter):
    sort: str = "created_at"
    thumb_w: int = 320


def finish_image(it: dict, w: int) -> dict:
    it.update(image_urls(it["id"], it.pop("file_id"), it.pop("mod_time"), w, it.pop("assets_rev")))
    return it


def _image_items(page, w: int) -> dict:
    for it in page.items:
        finish_image(it, w)
    return page_response(page)


async def images_by_id(pool, ids: list[int], acl: Acl, thumb_w: int) -> dict[int, dict]:
    rows = await pool.fetch(f"SELECT {IMAGE_SELECT} FROM {IMAGE_FROM} WHERE i.id = ANY($1::bigint[])", ids)
    return {r["id"]: finish_image(dict(r), thumb_w) for r in rows}


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
                          cursor=f.cursor, offset=f.offset, limit=f.limit, with_total=f.with_total)
    return _image_items(page, f.thumb_w)


class ImageIdsFilter(ImageFilter):
    gallery_id: int | None = None   # the filter model forbids unknown params, so it lives here


@router.get("/ids/images")
async def image_ids_matching(request: Request, f: Annotated[ImageIdsFilter, Query()],
                             acl: Acl = Depends(get_acl)) -> dict:
    pool = request.app.state.pool
    f = await expand_hierarchies(pool, f)
    q = Q()
    where = [IMAGE_PRESENT, *content_where(q, f, "image", "i", acl), editable_clause(acl, "i", q)]
    if f.gallery_id is not None:
        where.append(f"EXISTS (SELECT 1 FROM gallery_images gi WHERE gi.image_id = i.id "
                     f"AND gi.gallery_id = {q.p(f.gallery_id)})")
    return await _matching_ids(pool, IMAGE_FROM, "i", where, q.args)


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
                          offset=f.offset, limit=f.limit, with_total=f.with_total)
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
    out.update(image_urls(image_id, out.pop("file_id"), out.pop("mod_time"), 1280, out.pop("assets_rev")))
    del out["studio_id"]
    return out
