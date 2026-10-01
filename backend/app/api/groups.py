"""Scenes and images grouped by performer.

One page = some performers (by name), each with how many matching items they have and
their first few. Only performers with at least one item that matches the filters *and* is
visible to the viewer appear. Items without any performer come as `unassigned` on the last
page. An item with several performers appears under each of them.

A fixed number of round-trips per page regardless of size: a bounded probe that picks the
strategy, the groups (performers, totals, first item ids), and those items in list shape
(plus one for `unassigned` on the last page).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field

from app.acl import Acl, visible
from app.api import browse
from app.api.entities import entity_image_url
from app.auth import get_acl
from app.sql import Q, Sort, decode_cursor, encode_cursor

router = APIRouter(prefix="/groups", tags=["browse"])


class SceneGroupFilter(browse.SceneFilter):
    limit: int = Field(20, ge=1, le=50, description="performers per page")
    per_group: int = Field(12, ge=1, le=30, description="items shown per performer")


class ImageGroupFilter(browse.ImageFilter):
    limit: int = Field(20, ge=1, le=50)
    per_group: int = Field(12, ge=1, le=30)


# Below this many matching items it is cheaper to start from the items and group them;
# above it, to walk performers by name and look their items up.
SPARSE_LIMIT = 5000


async def _grouped(pool, acl: Acl, f, *, from_: str, link_from: str, alias: str, where_fn,
                   sorts: dict[str, Sort], by_id) -> dict:
    """`from_` selects the items; `link_from` is the same joined through the performer
    link table (alias `l`), so one performer's items come from a btree lookup."""
    if f.sort not in sorts:
        raise HTTPException(422, f"sort must be one of {sorted(sorts)}")
    s = sorts[f.sort]
    direction = "DESC" if f.desc else "ASC"
    key = f"{s.expr} COLLATE {s.collate}" if s.collate else s.expr
    k = f"t.k COLLATE {s.collate}" if s.collate else "t.k"

    def summary(q: Q, source: str) -> str:
        """total + first `per_group` ids of the rows produced by `source` (columns id, k)."""
        return f"""SELECT count(*) AS total,
                          (array_agg(t.id ORDER BY {k} {direction}, t.id {direction}))[1:{q.p(f.per_group, 'per')}] AS ids
                   FROM ({source}) t"""

    q = Q()
    wsql = " AND ".join(where_fn(q)) or "true"
    pconds = visible(acl, "performer", "pf", q)
    if f.cursor:
        name, last_id = decode_cursor(f.cursor, "group", False)
        pconds.append(f"(pf.name COLLATE natural_sort, pf.id) > "
                      f"({q.p(name)}::text COLLATE natural_sort, {q.p(last_id)}::bigint)")
    page = f"ORDER BY pf.name COLLATE natural_sort, pf.id LIMIT {q.p(f.limit + 1)}"

    probe_q = Q()
    probe_w = " AND ".join(where_fn(probe_q)) or "true"
    matches = await pool.fetchval(
        f"SELECT count(*) FROM (SELECT 1 FROM {from_} WHERE {probe_w} LIMIT {SPARSE_LIMIT + 1}) x", *probe_q.args)

    if matches <= SPARSE_LIMIT:
        # Few matches: group the matching items by performer, then page the performers.
        sql = f"""
            WITH m AS (SELECT {alias}.id, {alias}.performer_ids, {key} AS k FROM {from_} WHERE {wsql}),
                 pm AS (SELECT pid, count(*) AS total,
                               (array_agg(t.id ORDER BY {k} {direction}, t.id {direction}))[1:{q.p(f.per_group, 'per')}] AS ids
                        FROM m t, unnest(t.performer_ids) AS pid GROUP BY pid)
            SELECT pf.id, pf.name, pf.image_path, pf.updated_at, pm.total, pm.ids
            FROM pm JOIN performers pf ON pf.id = pm.pid
            WHERE {' AND '.join(pconds) or 'true'}
            {page}"""
    else:
        # Many matches: walk performers by name; each one's items via the link table.
        pconds.append(f"EXISTS (SELECT 1 FROM {link_from} WHERE l.performer_id = pf.id AND {wsql})")
        of_group = f"SELECT {alias}.id, {key} AS k FROM {link_from} WHERE l.performer_id = g.id AND {wsql}"
        sql = f"""
            WITH g AS (SELECT pf.id, pf.name, pf.image_path, pf.updated_at FROM performers pf
                       WHERE {' AND '.join(pconds)} {page})
            SELECT g.*, x.total, x.ids
            FROM g, LATERAL ({summary(q, of_group)}) x
            ORDER BY g.name COLLATE natural_sort, g.id"""
    rows = await pool.fetch(sql, *q.args)
    more = len(rows) > f.limit
    rows = rows[:f.limit]

    unassigned = None
    if not more:
        uq = Q()
        uw = " AND ".join(where_fn(uq)) or "true"
        none = f"SELECT {alias}.id, {key} AS k FROM {from_} WHERE cardinality({alias}.performer_ids) = 0 AND {uw}"
        u = await pool.fetchrow(summary(uq, none), *uq.args)
        if u["total"]:
            unassigned = {"total": u["total"], "ids": list(u["ids"] or [])}

    wanted = [i for r in rows for i in (r["ids"] or [])] + (unassigned["ids"] if unassigned else [])
    items = await by_id(pool, list(dict.fromkeys(wanted)), acl, f.thumb_w) if wanted else {}

    def materialize(ids):
        # each group gets its own copies: the same item can sit under several performers
        return [dict(items[i]) for i in ids if i in items]

    return {
        "groups": [{
            "performer": {"id": r["id"], "name": r["name"],
                          "image_url": entity_image_url("performers", r["id"], r["image_path"], r["updated_at"])},
            "total": r["total"],
            "items": materialize(r["ids"] or []),
        } for r in rows],
        "next_cursor": encode_cursor("group", False, rows[-1]["name"], rows[-1]["id"]) if more else None,
        "unassigned": {"total": unassigned["total"], "items": materialize(unassigned["ids"])} if unassigned else None,
    }


SCENE_LINK_FROM = "scene_performers l JOIN scenes s ON s.id = l.scene_id JOIN files f ON f.id = s.file_id"
IMAGE_LINK_FROM = f"image_performers l JOIN images i ON i.id = l.image_id {browse.IMAGE_JOINS}"


@router.get("/scenes")
async def scenes_by_performer(request: Request, f: Annotated[SceneGroupFilter, Query()],
                              acl: Acl = Depends(get_acl)) -> dict:
    browse._check_thumb(f.thumb_w)
    pool = request.app.state.pool
    f = await browse.expand_hierarchies(pool, f)
    return await _grouped(pool, acl, f, from_=browse.SCENE_FROM, link_from=SCENE_LINK_FROM, alias="s",
                          where_fn=lambda q: browse.scene_where(q, f, acl),
                          sorts=browse.SCENE_SORTS, by_id=browse.scenes_by_id)


@router.get("/images")
async def images_by_performer(request: Request, f: Annotated[ImageGroupFilter, Query()],
                              acl: Acl = Depends(get_acl)) -> dict:
    browse._check_thumb(f.thumb_w)
    pool = request.app.state.pool
    f = await browse.expand_hierarchies(pool, f)
    return await _grouped(pool, acl, f, from_=browse.IMAGE_FROM, link_from=IMAGE_LINK_FROM, alias="i",
                          where_fn=lambda q: [browse.IMAGE_PRESENT, *browse.content_where(q, f, "image", "i", acl)],
                          sorts=browse.IMAGE_SORTS, by_id=browse.images_by_id)
