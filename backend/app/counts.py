"""Recompute the entity_counts cache (see 0005_entity_counts.sql)."""

import asyncpg

from app import jobs
from app.acl import UNRESTRICTED, Acl, load_acl, visible
from app.sql import Q

JOB_KIND = "refresh_counts"
DEDUPE_KEY = "refresh_counts"
DEBOUNCE_SECONDS = 2.0


async def request_refresh(db) -> None:
    """Queue a refresh; bursts of edits within the debounce window collapse into one."""
    await jobs.enqueue(db, JOB_KIND, {}, dedupe_key=DEDUPE_KEY, delay=DEBOUNCE_SECONDS)


def _counts_sql(acl: Acl, q: Q) -> str:
    def where(conds: list[str]) -> str:
        return " AND ".join(conds) or "true"

    scenes = where(["f.missing_since IS NULL", "NOT s.is_live_photo", *visible(acl, "scene", "s", q)])
    galleries = where(["g.image_count > 0", *visible(acl, "gallery", "g", q)])
    images = where(["f.missing_since IS NULL", "z.missing_since IS NULL", *visible(acl, "image", "i", q)])
    parts = []
    for src, alias, cond, col in (
        ("scenes s JOIN files f ON f.id = s.file_id", "s", scenes, "scenes"),
        ("galleries g", "g", galleries, "galleries"),
        ("images i JOIN files f ON f.id = i.file_id LEFT JOIN files z ON z.id = f.zip_file_id", "i", images, "images"),
    ):
        one = {c: "1" if c == col else "0" for c in ("scenes", "galleries", "images")}
        vals = f"{one['scenes']}, {one['galleries']}, {one['images']}"
        parts += [
            f"SELECT 'tag', unnest({alias}.tag_ids), {vals} FROM {src} WHERE {cond}",
            f"SELECT 'performer', unnest({alias}.performer_ids), {vals} FROM {src} WHERE {cond}",
            f"SELECT 'studio', {alias}.studio_id, {vals} FROM {src} WHERE {cond} AND {alias}.studio_id IS NOT NULL",
        ]
    union = "\nUNION ALL ".join(parts)
    return f"""SELECT kind, id, sum(sc)::int, sum(gc)::int, sum(ic)::int
               FROM ({union}) x(kind, id, sc, gc, ic) GROUP BY kind, id"""


async def refresh(pool: asyncpg.Pool) -> int:
    """Rebuild counts for every active user. Users who see exactly the same content
    (same deny rules and hidden libraries) share one computation."""
    users = await pool.fetch("SELECT id, role FROM users WHERE is_active ORDER BY id")
    groups: dict[tuple, tuple[Acl, list[int]]] = {UNRESTRICTED.signature(): (UNRESTRICTED, [0])}
    for u in users:
        acl = await load_acl(pool, u["id"], deny_rules=u["role"] != "admin")
        groups.setdefault(acl.signature(), (acl, []))[1].append(u["id"])
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM entity_counts")
        for acl, viewers in groups.values():
            q = Q()
            v = q.p(viewers)
            await conn.execute(
                f"""WITH c AS MATERIALIZED ({_counts_sql(acl, q)})
                    INSERT INTO entity_counts (viewer, kind, entity_id, scenes, galleries, images)
                    SELECT viewer, c.* FROM c, unnest({v}::bigint[]) AS viewer""",
                *q.args,
            )
    return len(users)


async def refresh_viewer(pool: asyncpg.Pool, user_id: int, deny_rules: bool) -> None:
    """Recompute one user's counts right away (after they change their own filters), so
    the next page they open is already consistent."""
    acl = await load_acl(pool, user_id, deny_rules=deny_rules)
    q = Q()
    v = q.p(user_id)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM entity_counts WHERE viewer = $1", user_id)
        await conn.execute(
            f"""WITH c AS MATERIALIZED ({_counts_sql(acl, q)})
                INSERT INTO entity_counts (viewer, kind, entity_id, scenes, galleries, images)
                SELECT {v}::bigint, c.* FROM c""", *q.args)
