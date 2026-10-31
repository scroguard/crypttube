"""Folders linked to performers and studios: a file arriving in a linked folder (or below
it) is tagged with everyone linked to that folder.

`apply` is the only place tags get written, for the scanner, the Move dialog and the
back-tagging job. It only ever adds: a performer already there is left alone and a studio
already set is never replaced, so running it twice changes nothing. It also records what it
added (folder_link_tags), which is what unlinking a folder may take back: `remove_tags`
touches nothing else, so tags set by hand and tags from before the records began stay,
unless the person unlinking asks for those too (`claim_untracked`).

A folder can have many performers but one studio, and folders holding different studios
can't nest (api/folder_links.py refuses it), so at most one studio ever covers a file.

A link applies only while the person who made it may still tag that library's content:
the library's owner, or for a main library an active editor or admin (same rule as
permissions.may_edit, checked here because no request is involved).
"""

import asyncio

import asyncpg

from app import counts
from app.jobs import JobContext

JOB_KIND = "folder_link_backfill"
REMOVE_JOB_KIND = "folder_link_remove"
BATCH = 200

MATCHING_LINKS = """
    SELECT w.id, w.performer_id, w.studio_id
    FROM files f
    JOIN folder_links w ON w.library_id = f.library_id AND starts_with(f.path, w.path || '/')
    JOIN library_paths l ON l.id = w.library_id
    JOIN users u ON u.id = w.created_by
    WHERE f.id = $1 AND f.zip_file_id IS NULL
      AND ($2::text IS NULL OR NOT starts_with($2, w.path || '/'))
      AND w.removing_at IS NULL
      AND u.is_active AND (l.owner_id = u.id OR (l.owner_id IS NULL AND u.role IN ('editor', 'admin')))
    FOR SHARE OF w"""


async def apply(conn: asyncpg.Connection, file_id: int, old_path: str | None = None,
                only_link: int | None = None) -> bool:
    """Tag what this file holds with the links covering it. Returns whether anything changed.

    `old_path` is where a moved file came from: links that already covered it are skipped,
    so a rename can't bring back a performer someone took off by hand. `only_link` limits
    the tags to that link (back-tagging one link mustn't apply the others)."""
    links = await conn.fetch(MATCHING_LINKS, file_id, old_path)
    links = [w for w in links if only_link in (None, w["id"])]
    if not links:
        return False

    scenes = [r["id"] for r in await conn.fetch("SELECT id FROM scenes WHERE file_id = $1", file_id)]
    galleries = [r["id"] for r in await conn.fetch("SELECT id FROM galleries WHERE zip_file_id = $1", file_id)]
    images = [r["id"] for r in await conn.fetch(
        """SELECT id FROM images WHERE file_id = $1
           UNION SELECT image_id FROM gallery_images WHERE gallery_id = ANY($2::bigint[])""", file_id, galleries)]
    changed = 0
    for kind, table, ids in (("scene", "scenes", scenes), ("gallery", "galleries", galleries), ("image", "images", images)):
        if not ids:
            continue
        for w in links:
            changed += await (_tag_performer if w["performer_id"] is not None else _tag_studio)(
                conn, w, kind, table, ids, file_id)
    return changed > 0


def _claimed(column: str) -> str:
    """Some link for this performer or studio put the tag on this item."""
    return f"""EXISTS (SELECT 1 FROM folder_link_tags t JOIN folder_links o ON o.id = t.link_id
                       WHERE t.kind = $4 AND t.content_id = x.i AND o.{column} = $2)"""


async def _tag_performer(conn, w, kind: str, _table: str, ids: list[int], file_id: int) -> int:
    """Add the tag and record it. A tag with no record is someone else's, so it isn't claimed."""
    return await conn.fetchval(
        f"""WITH x AS (SELECT unnest($1::bigint[]) AS i),
                 mine AS (SELECT i FROM x WHERE {_claimed("performer_id")} OR NOT EXISTS (
                              SELECT 1 FROM {kind}_performers h WHERE h.{kind}_id = x.i AND h.performer_id = $2)),
                 added AS (INSERT INTO {kind}_performers ({kind}_id, performer_id) SELECT i, $2 FROM mine
                           ON CONFLICT DO NOTHING RETURNING 1),
                 noted AS (INSERT INTO folder_link_tags (link_id, kind, content_id, file_id)
                           SELECT $3, $4, i, $5 FROM mine ON CONFLICT DO NOTHING)
            SELECT count(*) FROM added""", ids, w["performer_id"], w["id"], kind, file_id)


async def _tag_studio(conn, w, kind: str, table: str, ids: list[int], file_id: int) -> int:
    """As _tag_performer, and a studio already set is never replaced."""
    return await conn.fetchval(
        f"""WITH x AS (SELECT c.id AS i, c.studio_id FROM {table} c WHERE c.id = ANY($1::bigint[])),
                 mine AS (SELECT i FROM x WHERE x.studio_id IS NULL OR (x.studio_id = $2 AND {_claimed("studio_id")})),
                 added AS (UPDATE {table} c SET studio_id = $2 FROM mine
                           WHERE c.id = mine.i AND c.studio_id IS NULL RETURNING 1),
                 noted AS (INSERT INTO folder_link_tags (link_id, kind, content_id, file_id)
                           SELECT $3, $4, i, $5 FROM mine ON CONFLICT DO NOTHING)
            SELECT count(*) FROM added""", ids, w["studio_id"], w["id"], kind, file_id)


async def disown(conn: asyncpg.Connection, kind: str, ids: list[int], column: str, values: list[int]) -> None:
    """Someone set these tags on purpose, so they're theirs now, whichever link added them."""
    await conn.execute(
        f"""DELETE FROM folder_link_tags t USING folder_links w
            WHERE t.link_id = w.id AND t.kind = $1 AND t.content_id = ANY($2::bigint[])
              AND w.{column} = ANY($3::bigint[])""", kind, ids, values)


def _entity(link) -> tuple[str, int]:
    return ("performer_id", link["performer_id"]) if link["performer_id"] is not None else ("studio_id", link["studio_id"])


def _covered(column: str) -> str:
    """Another folder still linked to the same performer or studio accounts for this tag."""
    return f"""(EXISTS (SELECT 1 FROM folder_links o WHERE o.{column} = $2 AND o.id <> $1 AND o.removing_at IS NULL
                        AND o.library_id = f.library_id AND starts_with(f.path, o.path || '/'))
                OR EXISTS (SELECT 1 FROM folder_link_tags t2 JOIN folder_links o ON o.id = t2.link_id
                           WHERE t2.kind = t.kind AND t2.content_id = t.content_id
                             AND o.{column} = $2 AND o.id <> $1 AND o.removing_at IS NULL))"""


def _untracked(column: str) -> str:
    """Tags on what the folder's files hold that no link recorded: set by hand, or from
    before tags were recorded. Takes the link id, the performer or studio, the library and
    the folder with a trailing slash."""
    holds = (f"JOIN {{kind}}_performers j ON j.{{kind}}_id = c.id AND j.performer_id = $2"
             if column == "performer_id" else "")
    studio = "" if column == "performer_id" else "WHERE c.studio_id = $2"
    parts = [f"""SELECT '{kind}'::text AS kind, c.id AS content_id, {file} AS file_id
                 FROM {table} c {holds.format(kind=kind)} {via} {studio}"""
             for kind, table, file, via in (
                 ("scene", "scenes", "c.file_id", ""),
                 ("image", "images", "c.file_id", ""),
                 ("gallery", "galleries", "c.zip_file_id", ""),
                 ("image", "images", "g.zip_file_id",
                  "JOIN gallery_images gi ON gi.image_id = c.id JOIN galleries g ON g.id = gi.gallery_id"))]
    return f"""SELECT t.kind, t.content_id, t.file_id, {_covered(column)} AS covered
               FROM ({' UNION '.join(parts)}) t JOIN files f ON f.id = t.file_id
               WHERE f.library_id = $3 AND starts_with(f.path, $4) AND f.zip_file_id IS NULL
                 AND NOT EXISTS (SELECT 1 FROM folder_link_tags r JOIN folder_links o ON o.id = r.link_id
                                 WHERE r.kind = t.kind AND r.content_id = t.content_id AND o.{column} = $2)"""


async def impact(pool: asyncpg.Pool, link) -> dict:
    """What removing a link's tags would do, in files: `remove` lose the tag, `covered` keep
    it through another linked folder, and `untracked` are in the folder with a tag no link
    recorded, which stays unless they're asked for (`claim_untracked`)."""
    column, value = _entity(link)
    return dict(await pool.fetchrow(
        f"""WITH u AS ({_untracked(column)}),
                 r AS (SELECT t.file_id, {_covered(column)} AS covered
                       FROM folder_link_tags t JOIN files f ON f.id = t.file_id WHERE t.link_id = $1)
            SELECT (SELECT count(DISTINCT file_id) FROM r WHERE NOT covered) AS remove,
                   (SELECT count(*) FROM (SELECT file_id FROM r WHERE covered
                                          UNION SELECT file_id FROM u WHERE covered) c) AS covered,
                   (SELECT count(DISTINCT file_id) FROM u WHERE NOT covered) AS untracked""",
        link["id"], value, link["library_id"], link["path"] + "/"))


async def claim_untracked(conn: asyncpg.Connection, link) -> None:
    """Make the folder's untracked tags this link's, so `remove_tags` takes them too. Done
    in the request, so however long the job takes, a tag set after this has no record and
    stays."""
    column, value = _entity(link)
    await conn.execute(
        f"""INSERT INTO folder_link_tags (link_id, kind, content_id, file_id)
            SELECT $1::bigint, kind, content_id, file_id FROM ({_untracked(column)}) u WHERE NOT covered
            ON CONFLICT DO NOTHING""", link["id"], value, link["library_id"], link["path"] + "/")


async def _take_back(conn, link, kind: str, content_id: int, file_id: int) -> bool:
    """One record of a link being removed. Returns whether the tag came off."""
    column, value = _entity(link)
    covered = await conn.fetchval(
        f"""SELECT {_covered(column)} FROM files f, (SELECT $3::text AS kind, $4::bigint AS content_id) t
            WHERE f.id = $5""", link["id"], value, kind, content_id, file_id)
    if covered:
        # The tag stays, so a folder that covers it has to answer for it from now on.
        await conn.execute(
            f"""INSERT INTO folder_link_tags (link_id, kind, content_id, file_id)
                SELECT o.id, $1, $2, $3 FROM folder_links o JOIN files f ON f.id = $3
                WHERE o.{column} = $4 AND o.id <> $5 AND o.removing_at IS NULL AND o.library_id = f.library_id
                  AND starts_with(f.path, o.path || '/')
                ON CONFLICT DO NOTHING""", kind, content_id, file_id, value, link["id"])
    elif column == "performer_id":
        await conn.execute(f"DELETE FROM {kind}_performers WHERE {kind}_id = $1 AND performer_id = $2", content_id, value)
    else:
        table = {"scene": "scenes", "image": "images", "gallery": "galleries"}[kind]
        await conn.execute(f"UPDATE {table} SET studio_id = NULL WHERE id = $1 AND studio_id = $2", content_id, value)
    await conn.execute("DELETE FROM folder_link_tags WHERE link_id = $1 AND kind = $2 AND content_id = $3",
                       link["id"], kind, content_id)
    return not covered


async def remove_tags(pool: asyncpg.Pool, ctx: JobContext, link_id: int) -> str:
    """Take a link's own tags back, then delete the link. Each batch commits and removes its
    records, so a run that stops can simply be started again."""
    link = await pool.fetchrow("SELECT * FROM folder_links WHERE id = $1 AND removing_at IS NOT NULL", link_id)
    if link is None:
        return "the link has been removed"
    total = await pool.fetchval("SELECT count(*) FROM folder_link_tags WHERE link_id = $1", link_id)
    done = removed = 0
    while True:
        if await ctx.cancelled():
            if removed:
                await counts.request_refresh(pool)
            return f"stopped: {removed} tags removed so far"
        rows = await pool.fetch(
            f"SELECT kind, content_id, file_id FROM folder_link_tags WHERE link_id = $1 LIMIT {BATCH}", link_id)
        if not rows:
            break
        async with pool.acquire() as conn, conn.transaction():
            for r in rows:
                removed += await _take_back(conn, link, r["kind"], r["content_id"], r["file_id"])
        done += len(rows)
        await ctx.progress(min(1.0, done / max(total, 1)), f"checked {done}/{total} tags")
        await asyncio.sleep(0)
    await pool.execute("DELETE FROM folder_links WHERE id = $1", link_id)
    if removed:
        await counts.request_refresh(pool)
    return f"removed {removed} of {total} tags"


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
        await ctx.progress(min(1.0, done / max(total, 1)), f"checked {done}/{total} files")
        await asyncio.sleep(0)
    if tagged:
        await counts.request_refresh(pool)
    return f"tagged {tagged} of {total} files"
