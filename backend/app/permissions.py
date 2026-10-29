"""Who may change what.

- Content in a global library, and global tags/performers/studios: editors and admins.
- Content in a user library, and personal tags/performers/studios: their owner only
  (whatever the owner's role; and not editors/admins).
- Anything referenced (a tag put on a scene, a parent studio, ...) must be visible to the
  person referencing it, so ids can't be used to probe other users' personal metadata.
"""

import asyncpg
from fastapi import HTTPException

from app.acl import Acl, visible
from app.sql import Q

ENTITY_TABLES = {"tag": "tags", "performer": "performers", "studio": "studios"}


def may_edit(acl: Acl, owner_id: int | None) -> bool:
    return acl.editor if owner_id is None else owner_id == acl.user_id


def _forbidden() -> HTTPException:
    return HTTPException(403, "you don't have permission to edit this")


async def require_content_edit(pool: asyncpg.Pool, acl: Acl, table: str, ids: list[int]) -> None:
    """`table` is scenes / galleries / images. Callers check visibility first."""
    rows = await pool.fetch(
        f"""SELECT l.owner_id FROM {table} c LEFT JOIN library_paths l ON l.id = c.library_id
            WHERE c.id = ANY($1::bigint[])""", ids)
    if not all(may_edit(acl, r["owner_id"]) for r in rows):
        raise _forbidden()


async def require_entity_edit(pool: asyncpg.Pool, acl: Acl, kind: str, ids: list[int]) -> None:
    rows = await pool.fetch(
        f"SELECT owner_id FROM {ENTITY_TABLES[kind]} WHERE id = ANY($1::bigint[])", ids)
    if not all(may_edit(acl, r["owner_id"]) for r in rows):
        raise _forbidden()


def require_can_create(acl: Acl) -> int | None:
    """Returns the owner for a new tag/performer/studio: None (global) for editors,
    the user's id (personal) for users who can upload."""
    if acl.editor:
        return None
    if acl.can_upload:
        return acl.user_id
    raise HTTPException(403, "you don't have permission to create this")


async def require_visible(pool: asyncpg.Pool, acl: Acl, kind: str, ids: list[int | None]) -> None:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return
    q = Q()
    conds = [f"e.id = ANY({q.p(list(wanted))}::bigint[])", *visible(acl, kind, "e", q)]
    found = await pool.fetchval(
        f"SELECT count(*) FROM {ENTITY_TABLES[kind]} e WHERE {' AND '.join(conds)}", *q.args)
    if found != len(wanted):
        raise HTTPException(422, f"referenced {kind} does not exist")
