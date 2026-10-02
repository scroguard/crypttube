"""Read API for metadata entities: performers, studios, tags.

Content counts come from the entity_counts cache (refreshed by the worker within
seconds of changes), keyed per viewer so restricted users never see numbers that
reveal hidden content.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.acl import Acl, visible
from app.auth import get_acl
from app.permissions import may_edit
from app.api.browse import ListParams, like, page_response, tag_set
from app.sql import ListQuery, Q, Sort, paginate

router = APIRouter(tags=["browse"])


def entity_image_url(kind: str, entity_id: int, image_path: str | None, updated_at) -> str | None:
    if not image_path:
        return None
    return f"/api/{kind}/{entity_id}/image?v={int(updated_at.timestamp() * 1000)}"


def counts(q: Q, acl: Acl, kind: str, alias: str) -> tuple[str, str]:
    """(join, columns) reading cached counts for this viewer (see app/counts.py)."""
    viewer = q.p(acl.counts_viewer, "counts_viewer")
    join = (f"LEFT JOIN entity_counts ec ON ec.viewer = {viewer}::bigint "
            f"AND ec.kind = '{kind}' AND ec.entity_id = {alias}.id")
    cols = (", coalesce(ec.scenes, 0) AS scene_count, coalesce(ec.galleries, 0) AS gallery_count"
            ", coalesce(ec.images, 0) AS image_count")
    return join, cols


def _visible_or_404(row, what: str, acl: Acl):
    if row is None:
        raise HTTPException(404, f"{what} not found")
    out = dict(row)
    owner = out.pop("owner_id")
    out["can_edit"] = may_edit(acl, owner)
    out["personal"] = owner is not None and owner == acl.user_id
    return out


# ---------------------------------------------------------------------------
# Performers
# ---------------------------------------------------------------------------

def _is_favorite(q: Q, acl: Acl, alias: str) -> str:
    """Favourites are personal: is this performer one of the viewer's?"""
    return (f"EXISTS (SELECT 1 FROM user_favorite_performers uf WHERE uf.user_id = "
            f"{q.p(acl.user_id, 'acl_me')}::bigint AND uf.performer_id = {alias}.id)")


PERFORMER_SORTS = {
    "name": Sort("p.name", "text", collate="natural_sort"),
    "created_at": Sort("p.created_at", "timestamptz"),
    "rating": Sort("coalesce(p.rating, -1)", "smallint"),
    "birthdate": Sort("coalesce(p.birthdate, '0001-01-01'::date)", "date"),
}


class PerformerFilter(ListParams):
    sort: str = "name"
    desc: bool = False
    q: str | None = Field(None, max_length=200, description="name or alias contains")
    gender: Literal["female", "male", "transgender_female", "transgender_male",
                    "intersex", "non_binary"] | None = None
    favorite: bool | None = None
    tag_ids: list[int] = []
    include_sub_tags: bool = True
    rating_min: int | None = Field(None, ge=0, le=100)


@router.get("/performers")
async def list_performers(request: Request, f: Annotated[PerformerFilter, Query()],
                          acl: Acl = Depends(get_acl)) -> dict:
    def where(q: Q) -> list[str]:
        w = visible(acl, "performer", "p", q)
        if f.q:
            pat = like(q, f.q)
            w.append(f"(p.name ILIKE {pat} OR EXISTS (SELECT 1 FROM performer_aliases a "
                     f"WHERE a.performer_id = p.id AND a.alias ILIKE {pat}))")
        if f.gender:
            w.append(f"p.gender = {q.p(f.gender)}::gender")
        if f.favorite is not None:
            w.append(("" if f.favorite else "NOT ") + _is_favorite(q, acl, "p"))
        for t in f.tag_ids:
            w.append(f"EXISTS (SELECT 1 FROM performer_tags x WHERE x.performer_id = p.id "
                     f"AND x.tag_id = ANY({tag_set(q, [t], f.include_sub_tags)}))")  # few rows; no array needed
        if f.rating_min is not None:
            w.append(f"coalesce(p.rating, -1) >= {q.p(f.rating_min)}")
        return w

    def enrich(q: Q):
        join, cols = counts(q, acl, "performer", "p")
        return join, f"{cols}, {_is_favorite(q, acl, 'p')} AS favorite"

    lq = ListQuery(
        alias="p", from_="performers p",
        select="""p.id, p.name, p.disambiguation, p.gender, p.birthdate, p.rating,
                  p.image_path, p.updated_at""",
        sorts=PERFORMER_SORTS, where=where, enrich=enrich,
    )
    page = await paginate(request.app.state.pool, lq, sort=f.sort, desc=f.desc,
                          cursor=f.cursor, offset=f.offset, limit=f.limit, with_total=f.with_total)
    for it in page.items:
        it["image_url"] = entity_image_url("performers", it["id"], it.pop("image_path"), it.pop("updated_at"))
    return page_response(page)


@router.get("/performers/{performer_id}")
async def get_performer(performer_id: int, request: Request, acl: Acl = Depends(get_acl)) -> dict:
    q = Q()
    q.p(performer_id)
    cond = " AND ".join(["p.id = $1", *visible(acl, "performer", "p", q)])
    join, cols = counts(q, acl, "performer", "p")
    row = await request.app.state.pool.fetchrow(f"""
        SELECT p.*,
               ARRAY(SELECT alias FROM performer_aliases WHERE performer_id = p.id ORDER BY alias) AS aliases,
               (SELECT coalesce(jsonb_agg(jsonb_build_object('id', x.id, 'name', x.name)
                                          ORDER BY x.name COLLATE natural_sort), '[]')
                FROM performer_tags l JOIN tags x ON x.id = l.tag_id WHERE l.performer_id = p.id) AS tags,
               {_is_favorite(q, acl, "p")} AS favorite
               {cols}
        FROM performers p {join} WHERE {cond}""", *q.args)
    out = _visible_or_404(row, "performer", acl)
    out["image_url"] = entity_image_url("performers", performer_id, out.pop("image_path"), out["updated_at"])
    return out


class FavoriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    favorite: bool


@router.put("/performers/{performer_id}/favorite", status_code=204)
async def set_favorite(performer_id: int, body: FavoriteBody, request: Request,
                       acl: Acl = Depends(get_acl)) -> Response:
    """Mark or unmark a performer as one of *my* favourites. Any signed-in user may,
    for any performer they can see; it changes nothing for anyone else."""
    await get_performer(performer_id, request, acl)   # 404 unless visible
    pool = request.app.state.pool
    if body.favorite:
        await pool.execute(
            "INSERT INTO user_favorite_performers (user_id, performer_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            acl.user_id, performer_id)
    else:
        await pool.execute("DELETE FROM user_favorite_performers WHERE user_id = $1 AND performer_id = $2",
                           acl.user_id, performer_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Studios
# ---------------------------------------------------------------------------

STUDIO_SORTS = {
    "name": Sort("st.name", "text", collate="natural_sort"),
    "created_at": Sort("st.created_at", "timestamptz"),
}


class StudioFilter(ListParams):
    sort: str = "name"
    desc: bool = False
    q: str | None = Field(None, max_length=200)
    parent_id: int | None = None
    root_only: bool = False


@router.get("/studios")
async def list_studios(request: Request, f: Annotated[StudioFilter, Query()],
                       acl: Acl = Depends(get_acl)) -> dict:
    def where(q: Q) -> list[str]:
        w = visible(acl, "studio", "st", q)
        if f.q:
            w.append(f"st.name ILIKE {like(q, f.q)}")
        if f.parent_id is not None:
            w.append(f"st.parent_id = {q.p(f.parent_id)}")
        if f.root_only:
            w.append("st.parent_id IS NULL")
        return w

    def enrich(q: Q):
        joins = """LEFT JOIN LATERAL (SELECT jsonb_build_object('id', x.id, 'name', x.name) AS parent
                   FROM studios x WHERE x.id = p.parent_id) r_par ON true"""
        child_cond = " AND ".join(["c.parent_id = p.id", *visible(acl, "studio", "c", q)])
        cjoin, ccols = counts(q, acl, "studio", "p")
        return f"{joins} {cjoin}", (f", r_par.parent {ccols}"
                                    f", (SELECT count(*) FROM studios c WHERE {child_cond}) AS child_count")

    lq = ListQuery(alias="st", from_="studios st",
                   select="st.id, st.name, st.parent_id, st.rating, st.image_path, st.updated_at",
                   sorts=STUDIO_SORTS, where=where, enrich=enrich)
    page = await paginate(request.app.state.pool, lq, sort=f.sort, desc=f.desc,
                          cursor=f.cursor, offset=f.offset, limit=f.limit, with_total=f.with_total)
    for it in page.items:
        it["image_url"] = entity_image_url("studios", it["id"], it.pop("image_path"), it.pop("updated_at"))
        del it["parent_id"]
    return page_response(page)


@router.get("/studios/{studio_id}")
async def get_studio(studio_id: int, request: Request, acl: Acl = Depends(get_acl)) -> dict:
    q = Q()
    q.p(studio_id)
    cond = " AND ".join(["st.id = $1", *visible(acl, "studio", "st", q)])
    child_cond = " AND ".join(["c.parent_id = st.id", *visible(acl, "studio", "c", q)])
    join, cols = counts(q, acl, "studio", "st")
    row = await request.app.state.pool.fetchrow(f"""
        SELECT st.*,
               (SELECT jsonb_build_object('id', x.id, 'name', x.name) FROM studios x WHERE x.id = st.parent_id) AS parent,
               (SELECT coalesce(jsonb_agg(jsonb_build_object('id', c.id, 'name', c.name)
                                          ORDER BY c.name COLLATE natural_sort), '[]')
                FROM studios c WHERE {child_cond}) AS children
               {cols}
        FROM studios st {join} WHERE {cond}""", *q.args)
    out = _visible_or_404(row, "studio", acl)
    out["image_url"] = entity_image_url("studios", studio_id, out.pop("image_path"), out["updated_at"])
    del out["parent_id"]
    return out


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

TAG_SORTS = {
    "name": Sort("t.name", "text", collate="natural_sort"),
    "created_at": Sort("t.created_at", "timestamptz"),
}


class TagFilter(ListParams):
    sort: str = "name"
    desc: bool = False
    q: str | None = Field(None, max_length=200)
    parent_id: int | None = Field(None, description="direct children of this tag")
    root_only: bool = False


@router.get("/tags")
async def list_tags(request: Request, f: Annotated[TagFilter, Query()],
                    acl: Acl = Depends(get_acl)) -> dict:
    def where(q: Q) -> list[str]:
        w = visible(acl, "tag", "t", q)
        if f.q:
            w.append(f"t.name ILIKE {like(q, f.q)}")
        if f.parent_id is not None:
            w.append(f"EXISTS (SELECT 1 FROM tag_relations r WHERE r.child_id = t.id AND r.parent_id = {q.p(f.parent_id)})")
        if f.root_only:
            w.append("NOT EXISTS (SELECT 1 FROM tag_relations r WHERE r.child_id = t.id)")
        return w

    def enrich(q: Q):
        child_vis = " AND ".join(["r.parent_id = p.id", *visible(acl, "tag", "c", q)])
        join, cols = counts(q, acl, "tag", "p")
        return join, (f"{cols}, (SELECT count(*) FROM tag_relations r JOIN tags c ON c.id = r.child_id "
                      f"WHERE {child_vis}) AS child_count")

    lq = ListQuery(alias="t", from_="tags t",
                   select="t.id, t.name, t.description, t.image_path, t.updated_at",
                   sorts=TAG_SORTS, where=where, enrich=enrich)
    page = await paginate(request.app.state.pool, lq, sort=f.sort, desc=f.desc,
                          cursor=f.cursor, offset=f.offset, limit=f.limit, with_total=f.with_total)
    for it in page.items:
        it["image_url"] = entity_image_url("tags", it["id"], it.pop("image_path"), it.pop("updated_at"))
    return page_response(page)


@router.get("/tags/{tag_id}")
async def get_tag(tag_id: int, request: Request, acl: Acl = Depends(get_acl)) -> dict:
    q = Q()
    q.p(tag_id)
    cond = " AND ".join(["t.id = $1", *visible(acl, "tag", "t", q)])
    join, cols = counts(q, acl, "tag", "t")
    rel_vis = lambda a: " AND ".join(visible(acl, "tag", a, q)) or "true"  # noqa: E731
    row = await request.app.state.pool.fetchrow(f"""
        SELECT t.*,
               (SELECT coalesce(jsonb_agg(jsonb_build_object('id', x.id, 'name', x.name)
                                          ORDER BY x.name COLLATE natural_sort), '[]')
                FROM tag_relations r JOIN tags x ON x.id = r.parent_id
                WHERE r.child_id = t.id AND {rel_vis('x')}) AS parents,
               (SELECT coalesce(jsonb_agg(jsonb_build_object('id', x.id, 'name', x.name)
                                          ORDER BY x.name COLLATE natural_sort), '[]')
                FROM tag_relations r JOIN tags x ON x.id = r.child_id
                WHERE r.parent_id = t.id AND {rel_vis('x')}) AS children
               {cols},
               (SELECT count(*) FROM performer_tags x JOIN performers pf ON pf.id = x.performer_id
                WHERE x.tag_id = t.id AND {" AND ".join(visible(acl, "performer", "pf", q)) or "true"}) AS performer_count
        FROM tags t {join} WHERE {cond}""", *q.args)
    out = _visible_or_404(row, "tag", acl)
    out["image_url"] = entity_image_url("tags", tag_id, out.pop("image_path"), out["updated_at"])
    return out
