"""A user's own content filters: tags and performers they don't want to see.

Applied in app.acl exactly like an admin's restrictions, so a filtered tag hides the tags
under it and everything tagged with them, and a filtered performer hides the content they
appear in. Unlike restrictions, the user sets these and can switch them off at any time.
"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app import counts
from app.acl import load_acl, visible
from app.auth import User, require_user
from app.permissions import require_visible
from app.sql import Q

router = APIRouter(prefix="/me/filters", tags=["filters"])

KINDS = (("tag", "tags", "user_filter_tags", "tag_id"),
         ("performer", "performers", "user_filter_performers", "performer_id"))


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    tag_ids: list[int] = Field(default=[], max_length=2000)
    performer_ids: list[int] = Field(default=[], max_length=2000)


async def _base_acl(pool, user: User):
    """What the user may see with their own filters set aside (restrictions still apply):
    the things they can choose from, and may be shown the names of."""
    return await load_acl(pool, user.id, deny_rules=not user.is_admin, filters=False)


async def _current(pool, user: User) -> dict:
    acl = await _base_acl(pool, user)
    out: dict = {"enabled": await pool.fetchval(
        "SELECT coalesce((SELECT filters_enabled FROM user_settings WHERE user_id = $1), true)", user.id)}
    for kind, table, link, col in KINDS:
        q = Q()
        conds = [f"x.user_id = {q.p(user.id)}", *visible(acl, kind, "e", q)]
        rows = await pool.fetch(
            f"""SELECT e.id, e.name FROM {link} x JOIN {table} e ON e.id = x.{col}
                WHERE {' AND '.join(conds)} ORDER BY e.name COLLATE natural_sort""", *q.args)
        out[table] = [dict(r) for r in rows]
    return out


@router.get("")
async def get_filters(request: Request, user: User = Depends(require_user)) -> dict:
    return await _current(request.app.state.pool, user)


@router.put("")
async def set_filters(body: Filters, request: Request, user: User = Depends(require_user)) -> dict:
    pool = request.app.state.pool
    acl = await _base_acl(pool, user)
    await require_visible(pool, acl, "tag", body.tag_ids)
    await require_visible(pool, acl, "performer", body.performer_ids)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO user_settings (user_id, filters_enabled) VALUES ($1, $2)
               ON CONFLICT (user_id) DO UPDATE SET filters_enabled = EXCLUDED.filters_enabled, updated_at = now()""",
            user.id, body.enabled)
        for (_kind, _table, link, col), ids in zip(KINDS, (body.tag_ids, body.performer_ids)):
            await conn.execute(f"DELETE FROM {link} WHERE user_id = $1", user.id)
            await conn.execute(
                f"INSERT INTO {link} (user_id, {col}) SELECT $1, unnest($2::bigint[]) ON CONFLICT DO NOTHING",
                user.id, ids)
    # Counts shown next to tags/performers are cached per viewer.
    await counts.refresh_viewer(pool, user.id, not user.is_admin)
    return await _current(pool, user)
