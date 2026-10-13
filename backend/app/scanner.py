"""Filesystem scanner: walks a library (or a subtree of one) and reconciles it with the DB.

Guarantees:
- Original media is only ever read (and is mounted read-only anyway).
- Unchanged files (same path, size, mtime) cost zero DB round-trips and zero probes.
- Files that vanish are soft-marked missing, never deleted, so user metadata survives
  an unmounted drive or a file that is being moved.
- Moved/renamed files are matched by (oshash, size) and keep their scene/image rows.
- Files modified within `settle_seconds` are skipped (likely still being copied) and
  reported back so the caller can schedule a delayed re-scan.
"""

import asyncio
import fnmatch
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

from app import crypto, media
from app.jobs import JobContext

log = logging.getLogger(__name__)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
BATCH = 64


class ScanError(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    mod_time: datetime
    kind: str


@dataclass
class ScanStats:
    new: int = 0
    updated: int = 0
    moved: int = 0
    missing: int = 0
    reappeared: int = 0
    failed: int = 0
    unsettled: int = 0
    errors: list[str] = field(default_factory=list)
    # scenes whose content is new or changed, i.e. that need generated assets
    scene_ids: list[int] = field(default_factory=list)
    # images that are new or changed, i.e. that need thumbnails
    image_ids: list[int] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{v} {k}" for k, v in vars(self).items() if isinstance(v, int) and v]
        return ", ".join(parts) or "no changes"


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def walk(root: Path, library_root: Path, excludes: list[str], skip_dir: Path) -> list[Entry]:
    """Collect media files under root. Runs in a worker thread."""
    entries = []
    private = crypto.is_private(library_root)

    def excluded(p: str) -> bool:
        rel = os.path.relpath(p, library_root)
        return any(fnmatch.fnmatch(rel, pat) for pat in excludes)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".")
            and not _is_within(Path(dirpath, d), skip_dir)
            and not excluded(os.path.join(dirpath, d))
        ]
        for name in filenames:
            if name.startswith("."):
                continue
            kind = media.classify(name)
            if kind is None:
                continue
            path = os.path.join(dirpath, name)
            if excluded(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue  # vanished between listdir and stat
            mod_time = EPOCH + timedelta(microseconds=st.st_mtime_ns // 1000)
            # An encrypted upload is a little larger on disk; record the content's size, so
            # encrypting an existing file doesn't look like a change.
            size = crypto.plain_size(path, st.st_size) if private else st.st_size
            entries.append(Entry(path, size, mod_time, kind))
    return entries


class Scanner:
    def __init__(
        self,
        pool: asyncpg.Pool,
        generated_dir: Path,
        *,
        ctx: JobContext | None = None,
        settle_seconds: float = 10,
        probe_concurrency: int = 4,
    ):
        self.pool = pool
        self.generated_dir = generated_dir
        self.ctx = ctx
        self.settle_seconds = settle_seconds
        self.sem = asyncio.Semaphore(probe_concurrency)
        self.stats = ScanStats()
        self._folder_galleries: dict[str, int] = {}
        self._folder_locks: dict[str, asyncio.Lock] = {}
        self._affected_galleries: set[int] = set()

    async def scan(self, library_id: int, subpath: str | None = None) -> ScanStats:
        lib = await self.pool.fetchrow("SELECT * FROM library_paths WHERE id = $1", library_id)
        if lib is None:
            raise ScanError(f"library {library_id} does not exist")
        lib_root = Path(lib["path"])
        # Refuse to reconcile against an absent root: an unmounted drive would
        # otherwise look like every file was deleted.
        if not lib_root.is_dir():
            raise ScanError(f"library path {lib_root} is not available")

        root = Path(subpath) if subpath else lib_root
        if not _is_within(root, lib_root):
            raise ScanError(f"{root} is outside library {lib_root}")

        entries = await asyncio.to_thread(
            walk, root, lib_root, list(lib["exclude_patterns"]), self.generated_dir
        )
        existing = {
            r["path"]: r
            for r in await self.pool.fetch(
                """SELECT id, path, size, mod_time, missing_since FROM files
                   WHERE library_id = $1 AND zip_file_id IS NULL
                     AND (path = $2 OR starts_with(path, $3))""",
                library_id, str(root), str(root).rstrip("/") + "/",
            )
        }

        settle_cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.settle_seconds)
        seen = {e.path for e in entries}
        vanished = [r for p, r in existing.items() if p not in seen]
        new, changed, reappeared = [], [], []
        for e in entries:
            if e.mod_time > settle_cutoff:
                self.stats.unsettled += 1
                continue
            row = existing.get(e.path)
            if row is None:
                new.append(e)
            elif row["size"] != e.size or row["mod_time"] != e.mod_time:
                changed.append((e, row["id"]))
            elif row["missing_since"] is not None:
                reappeared.append(row["id"])

        if reappeared:
            await self.pool.execute(
                "UPDATE files SET missing_since = NULL WHERE id = ANY($1)", reappeared
            )
            self.stats.reappeared = len(reappeared)

        new_hashed = await self._hash_all(new)
        moved = await self._match_moves(new_hashed, vanished, library_id)
        new_hashed = [(e, h) for e, h in new_hashed if e.path not in moved]

        still_vanished = [
            r["id"] for r in vanished
            if r["missing_since"] is None and r["id"] not in moved.values()
        ]
        if still_vanished:
            await self.pool.execute(
                "UPDATE files SET missing_since = now() WHERE id = ANY($1)", still_vanished
            )
            self.stats.missing = len(still_vanished)

        by_path = {e.path: e for e in entries}
        work = (
            [(e, None, "new") for e, _ in new_hashed]
            + [(e, fid, "updated") for e, fid in changed]
            + [(by_path[p], fid, "moved") for p, fid in moved.items()]
        )
        hashes = {e.path: h for e, h in new_hashed}
        total = len(work)
        for i in range(0, total, BATCH):
            if self.ctx and await self.ctx.cancelled():
                break
            await asyncio.gather(*(
                self._process(e, fid, library_id, lib["create_folder_galleries"], hashes.get(e.path), how)
                for e, fid, how in work[i:i + BATCH]
            ))
            if self.ctx:
                await self.ctx.progress(min(1.0, (i + BATCH) / total), f"processed {min(total, i + BATCH)}/{total}")

        await self._finalize_galleries()
        return self.stats

    async def _hash_all(self, entries: list[Entry]) -> list[tuple[Entry, str | None]]:
        """Fingerprint new files, a few at a time. (Hashing everything at once put one
        blocked thread per file on the disk and drove the machine's load through the roof.)"""
        async def one(e: Entry):
            async with self.sem:
                try:
                    return e, await asyncio.to_thread(media.oshash, e.path)
                except OSError:
                    return e, None

        out: list[tuple[Entry, str | None]] = []
        for i in range(0, len(entries), BATCH):
            if self.ctx and await self.ctx.cancelled():
                break
            out += await asyncio.gather(*(one(e) for e in entries[i:i + BATCH]))
            if self.ctx:
                await self.ctx.progress(0.0, f"reading {len(out)}/{len(entries)} new files")
        return out

    async def _match_moves(self, new_hashed, vanished, library_id) -> dict[str, int]:
        """Pair new paths with vanished/missing files that have the same content hash."""
        hashes = [h for _, h in new_hashed if h]
        if not hashes:
            return {}
        # Only within libraries of the same owner: an upload identical to someone else's
        # missing file is a different file, not a move. Otherwise a scene and its
        # metadata could be pulled across the privacy boundary between owners.
        candidates = await self.pool.fetch(
            """SELECT f.id, f.size, f.oshash FROM files f
               JOIN library_paths l ON l.id = f.library_id
               WHERE f.zip_file_id IS NULL AND f.oshash = ANY($1)
                 AND (f.missing_since IS NOT NULL OR f.id = ANY($2))
                 AND l.owner_id IS NOT DISTINCT FROM
                     (SELECT owner_id FROM library_paths WHERE id = $3)""",
            hashes, [r["id"] for r in vanished], library_id,
        )
        pool: dict[tuple[str, int], list[int]] = {}
        for c in candidates:
            pool.setdefault((c["oshash"], c["size"]), []).append(c["id"])
        moved = {}
        for e, h in new_hashed:
            ids = pool.get((h, e.size))
            if ids:
                fid = ids.pop()
                await self.pool.execute(
                    """UPDATE files SET path = $2, library_id = $3, mod_time = $4, missing_since = NULL
                       WHERE id = $1""",
                    fid, e.path, library_id, e.mod_time,
                )
                moved[e.path] = fid
        return moved

    async def _process(self, e: Entry, file_id, library_id, folder_galleries, oshash, how):
        try:
            async with self.sem:
                if how == "updated":
                    oshash = await asyncio.to_thread(media.oshash, e.path)
                if e.kind == "video":
                    probe = media.parse_video_probe(await media.ffprobe(e.path))
                elif e.kind == "image":
                    probe = await asyncio.to_thread(media.probe_image, e.path)
                else:
                    probe = await asyncio.to_thread(media.read_archive, e.path)
            # Resolve the folder gallery *before* taking a connection for the
            # transaction: acquiring a second pool connection while holding one
            # deadlocks once every pooled connection is held by a waiting task.
            gallery_id = None
            if e.kind == "image" and folder_galleries:
                gallery_id = await self._folder_gallery(os.path.dirname(e.path), library_id)
            async with self.pool.acquire() as conn, conn.transaction():
                if file_id is None:
                    file_id = await conn.fetchval(
                        """INSERT INTO files (library_id, path, size, mod_time, oshash)
                           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
                        library_id, e.path, e.size, e.mod_time, oshash,
                    )
                elif how == "updated":
                    await conn.execute(
                        """UPDATE files SET size = $2, mod_time = $3, oshash = $4, missing_since = NULL
                           WHERE id = $1""",
                        file_id, e.size, e.mod_time, oshash,
                    )
                if e.kind == "video":
                    scene_id = await self._upsert_scene(conn, file_id, e.path, probe)
                    if how != "moved":
                        self.stats.scene_ids.append(scene_id)
                elif e.kind == "image":
                    await self._upsert_image(conn, file_id, e.path, probe, gallery_id)
                else:
                    await self._upsert_archive(conn, file_id, library_id, e, probe)
            setattr(self.stats, how, getattr(self.stats, how) + 1)
        except (media.ProbeError, OSError, ValueError, asyncpg.PostgresError) as exc:
            self._record_failure(e, exc)

    def _record_failure(self, e: Entry, exc: Exception) -> None:
        log.warning("failed to index %s: %s", e.path, exc)
        self.stats.failed += 1
        if len(self.stats.errors) < 20:
            self.stats.errors.append(f"{e.path}: {exc}")

    async def _upsert_scene(self, conn, file_id: int, path: str, p: dict) -> int:
        # Title defaults to the filename on first index only; later scans never touch it.
        return await conn.fetchval(
            """INSERT INTO scenes (file_id, duration, width, height, framerate, bitrate,
                                   video_codec, audio_codec, container, title)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
               ON CONFLICT (file_id) DO UPDATE SET
                   duration = EXCLUDED.duration, width = EXCLUDED.width, height = EXCLUDED.height,
                   framerate = EXCLUDED.framerate, bitrate = EXCLUDED.bitrate,
                   video_codec = EXCLUDED.video_codec, audio_codec = EXCLUDED.audio_codec,
                   container = EXCLUDED.container
               RETURNING id""",
            file_id, p["duration"], p["width"], p["height"], p["framerate"], p["bitrate"],
            p["video_codec"], p["audio_codec"], p["container"], _stem(path),
        )

    async def _upsert_image(self, conn, file_id, path, info: media.ImageInfo, gallery_id: int | None) -> None:
        image_id = await conn.fetchval(
            """INSERT INTO images (file_id, width, height, format, title) VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (file_id) DO UPDATE SET
                   width = EXCLUDED.width, height = EXCLUDED.height, format = EXCLUDED.format
               RETURNING id""",
            file_id, info.width, info.height, info.format, _stem(path),
        )
        self.stats.image_ids.append(image_id)
        if gallery_id is None:
            return
        # A moved image leaves its old folder gallery.
        old = await conn.fetch(
            """DELETE FROM gallery_images gi USING galleries g
               WHERE gi.image_id = $1 AND gi.gallery_id = g.id
                 AND g.folder_path IS NOT NULL AND g.id <> $2
               RETURNING gi.gallery_id""",
            image_id, gallery_id,
        )
        await conn.execute(
            "INSERT INTO gallery_images (gallery_id, image_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            gallery_id, image_id,
        )
        self._affected_galleries.update([gallery_id, *(r["gallery_id"] for r in old)])

    async def _folder_gallery(self, folder: str, library_id: int) -> int:
        """Get or create the gallery for a folder. Runs in its own autocommit statement
        under a per-folder lock: concurrent image transactions would otherwise race on
        the insert, and the loser's snapshot can't see the winner's row."""
        if folder in self._folder_galleries:
            return self._folder_galleries[folder]
        async with self._folder_locks.setdefault(folder, asyncio.Lock()):
            if folder not in self._folder_galleries:
                gallery_id = await self.pool.fetchval(
                    """INSERT INTO galleries (folder_path, title, library_id) VALUES ($1, $2, $3)
                       ON CONFLICT (folder_path) DO NOTHING RETURNING id""",
                    folder, os.path.basename(folder), library_id,
                ) or await self.pool.fetchval("SELECT id FROM galleries WHERE folder_path = $1", folder)
                self._folder_galleries[folder] = gallery_id
        return self._folder_galleries[folder]

    async def _upsert_archive(self, conn, zip_file_id, library_id, e: Entry, probe) -> None:
        members, bad = probe
        self.stats.failed += bad
        gallery_id = await conn.fetchval(
            """WITH ins AS (
                   INSERT INTO galleries (zip_file_id, title) VALUES ($1, $2)
                   ON CONFLICT (zip_file_id) DO NOTHING RETURNING id)
               SELECT id FROM ins UNION ALL SELECT id FROM galleries WHERE zip_file_id = $1
               LIMIT 1""",
            zip_file_id, Path(e.path).stem,
        )
        existing = {
            r["path"]: r["id"]
            for r in await conn.fetch("SELECT id, path FROM files WHERE zip_file_id = $1", zip_file_id)
        }
        current = {m.path for m in members}
        gone = [fid for p, fid in existing.items() if p not in current]
        if gone:
            await conn.execute("DELETE FROM files WHERE id = ANY($1)", gone)
        for m in members:
            member_file_id = existing.get(m.path)
            if member_file_id is None:
                member_file_id = await conn.fetchval(
                    """INSERT INTO files (library_id, zip_file_id, path, size, mod_time)
                       VALUES ($1, $2, $3, $4, $5) RETURNING id""",
                    library_id, zip_file_id, m.path, m.size, e.mod_time,
                )
            else:
                await conn.execute(
                    "UPDATE files SET size = $2, mod_time = $3 WHERE id = $1",
                    member_file_id, m.size, e.mod_time,
                )
            image_id = await conn.fetchval(
                """INSERT INTO images (file_id, width, height, format, title) VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (file_id) DO UPDATE SET
                       width = EXCLUDED.width, height = EXCLUDED.height, format = EXCLUDED.format
                   RETURNING id""",
                member_file_id, m.info.width, m.info.height, m.info.format, _stem(m.path),
            )
            self.stats.image_ids.append(image_id)
            await conn.execute(
                "INSERT INTO gallery_images (gallery_id, image_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                gallery_id, image_id,
            )
        self._affected_galleries.add(gallery_id)

    async def _finalize_galleries(self) -> None:
        """Order filesystem-backed galleries by natural filename, pick a cover, drop empties."""
        ids = list(self._affected_galleries)
        if not ids:
            return
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """UPDATE gallery_images gi SET position = o.rn
                   FROM (SELECT gi2.gallery_id, gi2.image_id,
                                row_number() OVER (PARTITION BY gi2.gallery_id
                                                   ORDER BY f.path COLLATE natural_sort) AS rn
                         FROM gallery_images gi2
                         JOIN images i ON i.id = gi2.image_id
                         JOIN files f ON f.id = i.file_id
                         WHERE gi2.gallery_id = ANY($1)) o
                   WHERE gi.gallery_id = o.gallery_id AND gi.image_id = o.image_id
                     AND gi.position IS DISTINCT FROM o.rn""",
                ids,
            )
            await conn.execute(
                """DELETE FROM galleries
                   WHERE id = ANY($1) AND image_count = 0
                     AND (folder_path IS NOT NULL OR zip_file_id IS NOT NULL)""",
                ids,
            )
            await conn.execute(
                """UPDATE galleries g SET cover_image_id = (
                       SELECT image_id FROM gallery_images
                       WHERE gallery_id = g.id ORDER BY position, image_id LIMIT 1)
                   WHERE g.id = ANY($1)
                     AND (g.cover_image_id IS NULL OR NOT EXISTS (
                         SELECT 1 FROM gallery_images
                         WHERE gallery_id = g.id AND image_id = g.cover_image_id))""",
                ids,
            )
