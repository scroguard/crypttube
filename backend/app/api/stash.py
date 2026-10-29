"""Admin: import tags, performers and studios from a Stash database, with review.

1. The admin uploads the Stash SQLite file (or points at a copy in the media store). It
   is read once (app.stash_import), staged in stash_import_items, and discarded.
2. The admin reviews each kind and approves all of it or a selection. Only approved items
   are created; an item whose name already exists here is matched, never overwritten.
3. Finishing (or rejecting) deletes the staging rows.

Images are not taken from the Stash database. `POST /stash-import/images` instead assigns
pictures from a folder in the media store to entities by file name.
"""

import asyncio
import os
import re
import uuid
from urllib.parse import unquote
from pathlib import Path
from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import ClientDisconnect

from app import counts
from app.api.edit import MAX_SOURCE_IMAGE, _normalize_image
from app.api.libraries import _list_images
from app.auth import User, require_admin
from app.config import get_settings
from app.generated import ensure_dir
from app.stash_import import StashError, is_sqlite, read_stash

router = APIRouter(prefix="/stash-import", tags=["stash import"])

MAX_UPLOAD = 16 * 1024**3
Kind = Literal["tag", "performer", "studio"]
TABLES = {"tag": "tags", "performer": "performers", "studio": "studios"}

# How a staged item finds "the same thing" among this system's global (unowned) entities.
MATCH = {
    "tag": "lower(e.name) = lower(i.name) AND e.owner_id IS NULL",
    "studio": "lower(e.name) = lower(i.name) AND e.owner_id IS NULL",
    "performer": "lower(e.name) = lower(i.name) AND e.owner_id IS NULL "
                 "AND coalesce(e.disambiguation, '') = coalesce(i.data->>'disambiguation', '')",
}
# What the review list shows about an item (the rest is long text and internal ids).
LIST_DATA = """jsonb_strip_nulls(jsonb_build_object(
    'description', i.data->'description', 'parents', i.data->'parents', 'parent', i.data->'parent',
    'disambiguation', i.data->'disambiguation', 'gender', i.data->'gender', 'aliases', i.data->'aliases',
    'tags', i.data->'tags', 'url', i.data->'url'))"""


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _current(db) -> asyncpg.Record:
    row = await db.fetchrow("SELECT * FROM stash_imports ORDER BY id DESC LIMIT 1")
    if row is None:
        raise HTTPException(404, "no import is waiting for review")
    return row


async def _summary(db) -> dict | None:
    row = await db.fetchrow("SELECT * FROM stash_imports ORDER BY id DESC LIMIT 1")
    if row is None:
        return None
    kinds = {}
    for kind, table in TABLES.items():
        r = await db.fetchrow(
            f"""SELECT count(*) AS total,
                       count(*) FILTER (WHERE i.decision = 'imported') AS imported,
                       count(*) FILTER (WHERE i.decision IS DISTINCT FROM 'imported' AND m.found) AS existing
                FROM stash_import_items i
                LEFT JOIN LATERAL (SELECT true AS found FROM {table} e WHERE {MATCH[kind]} LIMIT 1) m ON true
                WHERE i.import_id = $1 AND i.kind = $2""", row["id"], kind)
        kinds[kind] = dict(r) | {"pending": r["total"] - r["imported"] - r["existing"]}
    return {"id": row["id"], "filename": row["filename"], "stash_version": row["stash_version"],
            "created_at": row["created_at"], "kinds": kinds}


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def _staged_rows(import_id: int, stash: dict) -> list[tuple]:
    rows = []
    for kind in TABLES:
        for item in stash[kind + "s"]:
            data = item["data"]
            if kind == "tag" and data["aliases"]:
                # Tags have no aliases here; keep them where they can still be found.
                also = "Also known as: " + ", ".join(data["aliases"])
                data["description"] = f"{data['description']}\n\n{also}" if data["description"] else also
            rows.append((import_id, kind, item["source_id"], item["name"], item["usage"], data))
    return rows


async def _stage(request: Request, user: User, path: Path, filename: str | None) -> dict:
    pool = request.app.state.pool
    try:
        stash = await asyncio.to_thread(read_stash, str(path))
    except StashError as e:
        raise HTTPException(422, str(e)) from e
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("LOCK TABLE stash_imports IN EXCLUSIVE MODE")
        if await conn.fetchval("SELECT true FROM stash_imports LIMIT 1"):
            raise HTTPException(409, "an import is already waiting for review; finish or reject it first")
        import_id = await conn.fetchval(
            "INSERT INTO stash_imports (created_by, filename, stash_version) VALUES ($1, $2, $3) RETURNING id",
            user.id, filename, stash["version"])
        await conn.executemany(
            """INSERT INTO stash_import_items (import_id, kind, source_id, name, usage, data)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb) ON CONFLICT DO NOTHING""",
            _staged_rows(import_id, stash))
        await conn.execute("ANALYZE stash_import_items")
    return await _summary(pool)


@router.get("")
async def current_import(request: Request) -> dict | None:
    return await _summary(request.app.state.pool)


@router.post("/upload", status_code=201)
async def upload(request: Request, user: User = Depends(require_admin)) -> dict:
    """The Stash database file as the raw request body."""
    if await request.app.state.pool.fetchval("SELECT true FROM stash_imports LIMIT 1"):
        raise HTTPException(409, "an import is already waiting for review; finish or reject it first")
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD:
        raise HTTPException(413, "file too large")
    tmp_dir = get_settings().generated_dir / "tmp"
    await asyncio.to_thread(ensure_dir, tmp_dir)
    tmp = tmp_dir / f"stash-{uuid.uuid4().hex}.sqlite"
    try:
        size = 0
        with open(tmp, "wb") as f:
            try:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise HTTPException(413, "file too large")
                    await asyncio.to_thread(f.write, chunk)
            except ClientDisconnect:
                raise HTTPException(400, "upload interrupted") from None
        name = os.path.basename(unquote(request.headers.get("x-filename") or ""))[:200] or None
        return await _stage(request, user, tmp, name)
    finally:
        tmp.unlink(missing_ok=True)


class FromPath(Body):
    path: str = Field(min_length=1, max_length=4000)


def _media_path(raw: str) -> Path:
    if not raw.startswith("/"):
        raise HTTPException(422, "path must be absolute")
    media_root = get_settings().media_dir.resolve()
    path = Path(raw).resolve()
    if not path.is_relative_to(media_root):
        raise HTTPException(422, f"path must be inside {media_root}")
    return path


@router.post("/path", status_code=201)
async def from_path(body: FromPath, request: Request, user: User = Depends(require_admin)) -> dict:
    """Read a copy of the Stash database that was placed in the (read-only) media store."""
    path = _media_path(body.path)
    if not path.is_file():
        raise HTTPException(404, "file not found")
    if not is_sqlite(str(path)):
        raise HTTPException(422, "that file is not an SQLite database")
    return await _stage(request, user, path, path.name)


@router.delete("", status_code=204)
async def finish(request: Request) -> Response:
    """Finish or reject: drop the staged data. Items already imported stay."""
    await request.app.state.pool.execute("DELETE FROM stash_imports")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

@router.get("/items")
async def items(kind: Kind, request: Request) -> list[dict]:
    pool = request.app.state.pool
    imp = await _current(pool)
    rows = await pool.fetch(
        f"""SELECT i.source_id, i.name, i.usage, {LIST_DATA} AS data,
                   CASE WHEN i.decision = 'imported' THEN 'imported'
                        WHEN m.id IS NOT NULL THEN 'existing' ELSE 'pending' END AS status,
                   coalesce(i.target_id, m.id) AS target_id
            FROM stash_import_items i
            LEFT JOIN LATERAL (SELECT e.id FROM {TABLES[kind]} e WHERE {MATCH[kind]} LIMIT 1) m ON true
            WHERE i.import_id = $1 AND i.kind = $2
            ORDER BY i.name COLLATE natural_sort, i.source_id""", imp["id"], kind)
    return [dict(r) for r in rows]


class Apply(Body):
    kind: Kind
    # None = everything still waiting; otherwise the Stash ids that were ticked.
    source_ids: list[int] | None = Field(None, max_length=500_000)


SELECTED = "i.import_id = $1 AND i.kind = $2 AND i.decision IS NULL AND ($3::bigint[] IS NULL OR i.source_id = ANY($3))"

INSERTS = {
    "tag": f"""INSERT INTO tags (name, description)
               SELECT DISTINCT ON (lower(i.name)) i.name, i.data->>'description'
               FROM stash_import_items i WHERE {SELECTED}
               ORDER BY lower(i.name), i.source_id
               ON CONFLICT DO NOTHING RETURNING id""",
    "studio": f"""INSERT INTO studios (name, url, details, rating, custom_fields)
                  SELECT DISTINCT ON (lower(i.name)) i.name, i.data->>'url', i.data->>'details',
                         (i.data->>'rating')::smallint, coalesce(i.data->'custom_fields', '{{}}')
                  FROM stash_import_items i WHERE {SELECTED}
                  ORDER BY lower(i.name), i.source_id
                  ON CONFLICT DO NOTHING RETURNING id""",
    "performer": f"""INSERT INTO performers (name, disambiguation, gender, birthdate, deathdate, country, ethnicity,
                         eye_color, hair_color, height_cm, weight_kg, measurements, tattoos, piercings,
                         career_start, career_end, details, rating, custom_fields)
                     SELECT DISTINCT ON (lower(i.name), coalesce(d.disambiguation, ''))
                            i.name, d.disambiguation, d.gender::gender, d.birthdate, d.deathdate, d.country, d.ethnicity,
                            d.eye_color, d.hair_color, d.height_cm, d.weight_kg, d.measurements, d.tattoos, d.piercings,
                            d.career_start, d.career_end, d.details, d.rating, coalesce(d.custom_fields, '{{}}')
                     FROM stash_import_items i, jsonb_to_record(i.data) AS d(
                            disambiguation text, gender text, birthdate date, deathdate date, country text,
                            ethnicity text, eye_color text, hair_color text, height_cm smallint, weight_kg smallint,
                            measurements text, tattoos text, piercings text, career_start smallint,
                            career_end smallint, details text, rating smallint, custom_fields jsonb)
                     WHERE {SELECTED}
                     ORDER BY lower(i.name), coalesce(d.disambiguation, ''), i.source_id
                     ON CONFLICT DO NOTHING RETURNING id""",
}

TAG_RELATIONS = """
    SELECT DISTINCT p.target_id AS parent_id, c.target_id AS child_id
    FROM stash_import_items c
    CROSS JOIN LATERAL jsonb_array_elements_text(c.data->'parent_ids') pid
    JOIN stash_import_items p ON p.import_id = c.import_id AND p.kind = 'tag' AND p.source_id = pid::bigint
    WHERE c.import_id = $1 AND c.kind = 'tag' AND c.target_id IS NOT NULL AND p.target_id IS NOT NULL
      AND p.target_id <> c.target_id AND 'imported' IN (c.decision, p.decision)
      AND NOT EXISTS (SELECT 1 FROM tag_relations r WHERE r.parent_id = p.target_id AND r.child_id = c.target_id)"""

STUDIO_PARENTS = """
    SELECT c.target_id AS id, p.target_id AS parent_id
    FROM stash_import_items c
    JOIN stash_import_items p ON p.import_id = c.import_id AND p.kind = 'studio'
                             AND p.source_id = (c.data->>'parent_id')::bigint
    JOIN studios s ON s.id = c.target_id
    WHERE c.import_id = $1 AND c.kind = 'studio' AND c.decision = 'imported'
      AND p.target_id IS NOT NULL AND p.target_id <> c.target_id AND s.parent_id IS NULL"""


async def _each_tolerating_cycles(conn, bulk: str, select: str, single: str, import_id: int) -> None:
    """Run `bulk`; if a hierarchy cycle is rejected (possible when names merge with what
    was already here), redo it row by row and skip just the offending links."""
    try:
        async with conn.transaction():
            await conn.execute(bulk, import_id)
        return
    except asyncpg.CheckViolationError:
        pass
    for row in await conn.fetch(select, import_id):
        try:
            async with conn.transaction():
                await conn.execute(single, *row.values())
        except asyncpg.CheckViolationError:
            continue


async def _link(conn, import_id: int) -> None:
    """Connect everything this import has created so far. Safe to repeat, and independent
    of the order the kinds were approved in."""
    # Re-resolve every staged item against what exists right now, approved or not: an
    # imported tag should hang under a parent that was already here, and something
    # deleted since an earlier step must not be linked to.
    for kind, table in TABLES.items():
        await conn.execute(
            f"""UPDATE stash_import_items i
                SET target_id = (SELECT e.id FROM {table} e WHERE {MATCH[kind]} LIMIT 1)
                WHERE i.import_id = $1 AND i.kind = $2""", import_id, kind)
    await _each_tolerating_cycles(
        conn,
        f"INSERT INTO tag_relations (parent_id, child_id) {TAG_RELATIONS} ON CONFLICT DO NOTHING",
        TAG_RELATIONS,
        "INSERT INTO tag_relations (parent_id, child_id) VALUES ($1, $2) ON CONFLICT DO NOTHING", import_id)
    await _each_tolerating_cycles(
        conn,
        f"UPDATE studios s SET parent_id = x.parent_id FROM ({STUDIO_PARENTS}) x WHERE s.id = x.id",
        STUDIO_PARENTS,
        "UPDATE studios SET parent_id = $2 WHERE id = $1", import_id)
    await conn.execute(
        """INSERT INTO performer_tags (performer_id, tag_id)
           SELECT DISTINCT i.target_id, t.target_id
           FROM stash_import_items i
           CROSS JOIN LATERAL jsonb_array_elements_text(i.data->'tag_ids') tid
           JOIN stash_import_items t ON t.import_id = i.import_id AND t.kind = 'tag' AND t.source_id = tid::bigint
           WHERE i.import_id = $1 AND i.kind = 'performer' AND i.decision = 'imported'
             AND i.target_id IS NOT NULL AND t.target_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM performer_tags x
                             WHERE x.performer_id = i.target_id AND x.tag_id = t.target_id)
           ON CONFLICT DO NOTHING""", import_id)


@router.post("/apply")
async def apply(body: Apply, request: Request, user: User = Depends(require_admin)) -> dict:
    """Approve staged items of one kind: create the new ones, match the ones already here."""
    pool = request.app.state.pool
    kind, table = body.kind, TABLES[body.kind]
    async with pool.acquire() as conn, conn.transaction():
        imp = await conn.fetchrow("SELECT * FROM stash_imports ORDER BY id DESC LIMIT 1 FOR UPDATE")
        if imp is None:
            raise HTTPException(404, "no import is waiting for review")
        args = (imp["id"], kind, body.source_ids)
        created = [r["id"] for r in await conn.fetch(INSERTS[kind], *args)]
        marked = await conn.fetch(
            f"""UPDATE stash_import_items i
                SET target_id = e.id,
                    decision = CASE WHEN e.id = ANY($4::bigint[]) THEN 'imported' ELSE 'existing' END
                FROM {table} e
                WHERE {SELECTED} AND {MATCH[kind]}
                RETURNING i.decision""", *args, created)
        if kind == "performer" and created:
            await conn.execute(
                """INSERT INTO performer_aliases (performer_id, alias)
                   SELECT DISTINCT i.target_id, a
                   FROM stash_import_items i CROSS JOIN LATERAL jsonb_array_elements_text(i.data->'aliases') a
                   WHERE i.import_id = $1 AND i.kind = 'performer' AND i.target_id = ANY($2::bigint[])
                   ON CONFLICT DO NOTHING""", imp["id"], created)
            # Stash's favourites were global; here they are personal, so they become the importing admin's.
            await conn.execute(
                """INSERT INTO user_favorite_performers (user_id, performer_id)
                   SELECT DISTINCT $3::bigint, i.target_id FROM stash_import_items i
                   WHERE i.import_id = $1 AND i.kind = 'performer' AND i.target_id = ANY($2::bigint[])
                     AND (i.data->>'favorite')::boolean
                   ON CONFLICT DO NOTHING""", imp["id"], created, user.id)
        await _link(conn, imp["id"])
    await counts.request_refresh(pool)
    return {"created": len(created),
            "existing": sum(1 for r in marked if r["decision"] == "existing"),
            "import": await _summary(pool)}


# ---------------------------------------------------------------------------
# Images from a folder, matched by file name
# ---------------------------------------------------------------------------

class ImagesFromFolder(Body):
    kind: Literal["studios", "performers", "tags"] = "studios"
    path: str = Field(min_length=1, max_length=4000)
    overwrite: bool = False


def _key(name: str) -> str:
    """Names compare ignoring case, punctuation and spacing ("Some-Studio!" = "some studio")."""
    return " ".join(re.findall(r"[^\W_]+", name.lower()))


@router.post("/images")
async def images_from_folder(body: ImagesFromFolder, request: Request) -> dict:
    """Give each studio (or performer, or tag) the picture in `path` whose file name is its
    name, e.g. "Some Studio.png". The folder is only read; pictures are stored as WebP in
    the generated volume like any other entity image."""
    pool = request.app.state.pool
    folder = _media_path(body.path)
    if not folder.is_dir():
        raise HTTPException(404, "folder not found")
    try:
        files = await asyncio.to_thread(_list_images, folder)
    except PermissionError:
        raise HTTPException(403, "folder is not readable by the server") from None
    by_key: dict[str, dict] = {}
    for f in files:
        by_key.setdefault(_key(Path(f["name"]).stem), f)

    extra = ", disambiguation" if body.kind == "performers" else ", NULL AS disambiguation"
    entities = await pool.fetch(
        f"SELECT id, name, image_path{extra} FROM {body.kind} WHERE owner_id IS NULL ORDER BY id")
    generated_dir = get_settings().generated_dir
    used: set[str] = set()
    matched = failed = skipped = 0
    for e in entities:
        keys = [_key(f"{e['name']} {e['disambiguation']}")] if e["disambiguation"] else []
        keys.append(_key(e["name"]))
        file = next((by_key[k] for k in keys if k and k in by_key), None)
        if file is None:
            continue
        used.add(file["name"])
        if e["image_path"] and not body.overwrite:
            skipped += 1
            continue
        rel = f"entities/{body.kind}/{e['id']}.webp"

        def store(src=file, dest=generated_dir / rel) -> None:
            if src["size"] > MAX_SOURCE_IMAGE:
                raise HTTPException(413, "image too large")
            _normalize_image(Path(src["path"]).read_bytes(), dest)
        try:
            await asyncio.to_thread(store)
        except (HTTPException, OSError):
            failed += 1
            continue
        await pool.execute(f"UPDATE {body.kind} SET image_path = $2 WHERE id = $1", e["id"], rel)
        matched += 1
    unmatched = [f["name"] for f in files if f["name"] not in used]
    return {"files": len(files), "matched": matched, "skipped": skipped, "failed": failed,
            "unmatched": unmatched[:200], "unmatched_total": len(unmatched)}
