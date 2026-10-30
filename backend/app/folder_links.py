"""Folders linked to performers and studios: a file arriving in a linked folder (or below
it) is tagged with everyone linked to that folder.

`apply` is the only place tags get written, for the scanner, the Move dialog and the
back-tagging job. It only ever adds: a performer already there is left alone and a studio
already set is never replaced, so running it twice changes nothing.

Content has one studio, so when several linked studios match the file the one linked to
the deepest folder wins, then the one linked first.

A link applies only while the person who made it may still tag that library's content:
the library's owner, or for a main library an active editor or admin (same rule as
permissions.may_edit, checked here because no request is involved).
"""

import asyncio

import asyncpg

from app import counts
from app.jobs import JobContext

JOB_KIND = "folder_link_backfill"
BATCH = 200

MATCHING_LINKS = """
    SELECT w.id, w.performer_id, w.studio_id
    FROM files f
    JOIN folder_links w ON w.library_id = f.library_id AND starts_with(f.path, w.path || '/')
    JOIN library_paths l ON l.id = w.library_id
    JOIN users u ON u.id = w.created_by
    WHERE f.id = $1 AND f.zip_file_id IS NULL
      AND ($2::text IS NULL OR NOT starts_with($2, w.path || '/'))
      AND u.is_active AND (l.owner_id = u.id OR (l.owner_id IS NULL AND u.role IN ('editor', 'admin')))
    ORDER BY length(w.path) DESC, w.id"""


async def apply(conn: asyncpg.Connection, file_id: int, old_path: str | None = None,
                only_link: int | None = None) -> bool:
    """Tag what this file holds with the links covering it. Returns whether any link did.

    `old_path` is where a moved file came from: links that already covered it are skipped,
    so a rename can't bring back a performer someone took off by hand. `only_link` limits
    the tags to that link (back-tagging one link mustn't apply the others)."""
    links = await conn.fetch(MATCHING_LINKS, file_id, old_path)
    studio = next((w for w in links if w["studio_id"] is not None), None)
    performers = [w["performer_id"] for w in links if w["performer_id"] is not None
                  and only_link in (None, w["id"])]
    studio_id = studio["studio_id"] if studio and only_link in (None, studio["id"]) else None
    if not performers and studio_id is None:
        return False

    scenes = [r["id"] for r in await conn.fetch("SELECT id FROM scenes WHERE file_id = $1", file_id)]
    galleries = [r["id"] for r in await conn.fetch("SELECT id FROM galleries WHERE zip_file_id = $1", file_id)]
    images = [r["id"] for r in await conn.fetch(
        """SELECT id FROM images WHERE file_id = $1
           UNION SELECT image_id FROM gallery_images WHERE gallery_id = ANY($2::bigint[])""", file_id, galleries)]
    for table, column, ids in (("scene", "scenes", scenes), ("gallery", "galleries", galleries),
                               ("image", "images", images)):
        if not ids:
            continue
        if performers:
            await conn.execute(
                f"""INSERT INTO {table}_performers ({table}_id, performer_id)
                    SELECT i, p FROM unnest($1::bigint[]) i, unnest($2::bigint[]) p
                    ON CONFLICT DO NOTHING""", ids, performers)
        if studio_id is not None:
            await conn.execute(
                f"UPDATE {column} SET studio_id = $2 WHERE id = ANY($1::bigint[]) AND studio_id IS NULL",
                ids, studio_id)
    return True


async def backfill(pool: asyncpg.Pool, ctx: JobContext, link_id: int) -> str:
    """Tag the files already in a linked folder, a batch per transaction, so a big folder
    holds no locks for long and can be cancelled. Safe to run again at any point."""
    link = await pool.fetchrow("SELECT library_id, path FROM folder_links WHERE id = $1", link_id)
    if link is None:
        return "the link has been removed"
    prefix = link["path"] + "/"
    where = "library_id = $1 AND starts_with(path, $2) AND zip_file_id IS NULL AND missing_since IS NULL"
    total = await pool.fetchval(f"SELECT count(*) FROM files WHERE {where}", link["library_id"], prefix)
    done = tagged = last = 0
    while not await ctx.cancelled():
        ids = [r["id"] for r in await pool.fetch(
            f"SELECT id FROM files WHERE {where} AND id > $3 ORDER BY id LIMIT {BATCH}",
            link["library_id"], prefix, last)]
        if not ids:
            break
        async with pool.acquire() as conn, conn.transaction():
            for file_id in ids:
                tagged += await apply(conn, file_id, only_link=link_id)
        last, done = ids[-1], done + len(ids)
        await ctx.progress(done / total, f"checked {done}/{total} files")
        await asyncio.sleep(0)
    if tagged:
        await counts.request_refresh(pool)
    return f"tagged {tagged} of {total} files"
