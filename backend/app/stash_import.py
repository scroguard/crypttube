"""Read tags, performers and studios out of a Stash (stashapp) SQLite database.

The file is opened read-only and never modified. Stash's schema has changed over its
~85 migrations, so nothing here assumes a column exists: each query is built from the
columns the file actually has, and optional tables are simply skipped when absent.
Image blobs are ignored on purpose; this system generates its own.
"""

import re
import sqlite3
from datetime import date

SQLITE_MAGIC = b"SQLite format 3\x00"

GENDERS = {
    "FEMALE": "female", "MALE": "male",
    "TRANSGENDER_FEMALE": "transgender_female", "TRANSGENDER_MALE": "transgender_male",
    "INTERSEX": "intersex", "NON_BINARY": "non_binary",
}


class StashError(Exception):
    """The file isn't a Stash database we can read; the message is shown to the admin."""


def is_sqlite(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def _date(value) -> str | None:
    """A valid ISO date string, or None (Stash allows partial/odd dates)."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _year(value) -> int | None:
    m = re.match(r"\s*(\d{4})", str(value)) if value else None
    return int(m.group(1)) if m and 1800 <= int(m.group(1)) <= 2200 else None


def _text(value) -> str | None:
    if value is None:
        return None
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    return text.strip() or None


def _int(value, lo: int, hi: int) -> int | None:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


class _Db:
    def __init__(self, path: str):
        try:
            # immutable=1: no locks, no journal/WAL files created next to a read-only file
            self.con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
            self.con.row_factory = sqlite3.Row
            self.tables = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        except sqlite3.DatabaseError as e:
            raise StashError(f"not a readable SQLite database ({e})") from e

    def columns(self, table: str) -> set[str]:
        return {r[1] for r in self.con.execute(f'PRAGMA table_info("{table}")')} if table in self.tables else set()

    def rows(self, sql: str) -> list[sqlite3.Row]:
        return self.con.execute(sql).fetchall()

    def select(self, table: str, wanted: list[str]) -> list[dict]:
        """SELECT the wanted columns that exist; missing ones come back as None."""
        have = self.columns(table)
        cols = [c for c in wanted if c in have]
        rows = self.rows(f'SELECT {", ".join(f"`{c}`" for c in cols)} FROM "{table}"')
        return [{c: (r[c] if c in have else None) for c in wanted} for r in rows]

    def grouped(self, table: str, key: str, value: str, order: str | None = None) -> dict[int, list]:
        """{key: [values]} from a link/alias table, or {} if it doesn't exist."""
        if table not in self.tables or not {key, value} <= self.columns(table):
            return {}
        out: dict[int, list] = {}
        by = f" ORDER BY `{order}`" if order and order in self.columns(table) else ""
        for r in self.rows(f'SELECT `{key}`, `{value}` FROM "{table}"{by}'):
            if r[0] is not None and r[1] is not None:
                out.setdefault(r[0], []).append(r[1])
        return out

    def counts(self, table: str, key: str) -> dict[int, int]:
        if table not in self.tables or key not in self.columns(table):
            return {}
        return {r[0]: r[1] for r in self.rows(f'SELECT `{key}`, count(*) FROM "{table}" GROUP BY 1') if r[0] is not None}


def _scale_ratings(items: list[dict]) -> None:
    """Old Stash versions rated 1-5; newer ones (and this system) 0-100."""
    ratings = [i["data"]["rating"] for i in items if i["data"].get("rating") is not None]
    if ratings and max(ratings) <= 5:
        for i in items:
            if i["data"].get("rating") is not None:
                i["data"]["rating"] *= 20


def _sum(*maps: dict[int, int]):
    return lambda key: sum(m.get(key, 0) for m in maps)


def read_stash(path: str) -> dict:
    """{"version": int, "tags": [...], "performers": [...], "studios": [...]}; each entry
    is {"source_id", "name", "usage", "data"} with `data` already in this system's terms."""
    if not is_sqlite(path):
        raise StashError("that file is not an SQLite database")
    db = _Db(path)
    try:
        missing = {"tags", "performers", "studios"} - db.tables
        if missing:
            raise StashError("this doesn't look like a Stash database (missing tables: "
                             + ", ".join(sorted(missing)) + ")")
        version = None
        if "schema_migrations" in db.tables:
            row = db.con.execute("SELECT version FROM schema_migrations").fetchone()
            version = row[0] if row else None
        return {"version": version, "tags": _tags(db), "performers": _performers(db), "studios": _studios(db)}
    except sqlite3.DatabaseError as e:
        raise StashError(f"could not read the Stash database ({e})") from e
    finally:
        db.con.close()


def _tags(db: _Db) -> list[dict]:
    rows = db.select("tags", ["id", "name", "description"])
    names = {r["id"]: _text(r["name"]) for r in rows}
    parents = db.grouped("tags_relations", "child_id", "parent_id")
    aliases = db.grouped("tag_aliases", "tag_id", "alias")
    usage = _sum(db.counts("scenes_tags", "tag_id"), db.counts("images_tags", "tag_id"),
                 db.counts("galleries_tags", "tag_id"), db.counts("performers_tags", "tag_id"))
    out = []
    for r in rows:
        if not names[r["id"]]:
            continue
        parent_ids = [p for p in parents.get(r["id"], []) if names.get(p)]
        out.append({"source_id": r["id"], "name": names[r["id"]], "usage": usage(r["id"]), "data": {
            "description": _text(r["description"]),
            "aliases": sorted({a for a in map(_text, aliases.get(r["id"], [])) if a}),
            "parent_ids": parent_ids,
            "parents": [names[p] for p in parent_ids],
        }})
    return out


def _performers(db: _Db) -> list[dict]:
    rows = db.select("performers", [
        "id", "name", "disambiguation", "gender", "birthdate", "death_date", "ethnicity", "country",
        "eye_color", "hair_color", "height", "weight", "measurements", "fake_tits", "tattoos", "piercings",
        "career_start", "career_end", "career_length", "details", "rating", "favorite",
        "penis_length", "circumcised", "aliases", "url",
    ])
    aliases = db.grouped("performer_aliases", "performer_id", "alias")
    urls = db.grouped("performer_urls", "performer_id", "url", order="position")
    tags = db.grouped("performers_tags", "performer_id", "tag_id")
    tag_names = {r["id"]: _text(r["name"]) for r in db.select("tags", ["id", "name"])}
    usage = _sum(db.counts("performers_scenes", "performer_id"), db.counts("performers_images", "performer_id"),
                 db.counts("performers_galleries", "performer_id"))
    out = []
    for r in rows:
        name = _text(r["name"])
        if not name:
            continue
        # very old schemas kept aliases as one comma-separated column, and a single url
        alias_set = {a for a in map(_text, aliases.get(r["id"], [])) if a}
        alias_set |= {a.strip() for a in (_text(r["aliases"]) or "").split(",") if a.strip()}
        url_list = [u for u in map(_text, urls.get(r["id"], [])) if u] or ([_text(r["url"])] if _text(r["url"]) else [])
        career = _text(r["career_length"]) or ""
        extra = {k: v for k, v in {
            "urls": url_list or None,
            "fake_tits": _text(r["fake_tits"]),
            "penis_length": r["penis_length"],
            "circumcised": _text(r["circumcised"]),
        }.items() if v is not None}
        tag_ids = [t for t in tags.get(r["id"], []) if tag_names.get(t)]
        out.append({"source_id": r["id"], "name": name, "usage": usage(r["id"]), "data": {
            "disambiguation": _text(r["disambiguation"]),
            "gender": GENDERS.get((_text(r["gender"]) or "").upper()),
            "birthdate": _date(r["birthdate"]),
            "deathdate": _date(r["death_date"]),
            "ethnicity": _text(r["ethnicity"]),
            "country": _text(r["country"]),
            "eye_color": _text(r["eye_color"]),
            "hair_color": _text(r["hair_color"]),
            "height_cm": _int(r["height"], 1, 299),
            "weight_kg": _int(r["weight"], 1, 499),
            "measurements": _text(r["measurements"]),
            "tattoos": _text(r["tattoos"]),
            "piercings": _text(r["piercings"]),
            "career_start": _year(r["career_start"]) or _year(career),
            "career_end": _year(r["career_end"]) or (_year(career.split("-")[-1]) if "-" in career else None),
            "details": _text(r["details"]),
            "rating": _int(r["rating"], 0, 100),
            "favorite": bool(r["favorite"]),
            "aliases": sorted(alias_set - {name}),
            "custom_fields": extra,
            "tag_ids": tag_ids,
            "tags": [tag_names[t] for t in tag_ids],
        }})
    _scale_ratings(out)
    return out


def _studios(db: _Db) -> list[dict]:
    rows = db.select("studios", ["id", "name", "parent_id", "details", "rating", "url"])
    names = {r["id"]: _text(r["name"]) for r in rows}
    aliases = db.grouped("studio_aliases", "studio_id", "alias")
    urls = db.grouped("studio_urls", "studio_id", "url", order="position")
    usage = _sum(db.counts("scenes", "studio_id"), db.counts("images", "studio_id"), db.counts("galleries", "studio_id"))
    out = []
    for r in rows:
        if not names[r["id"]]:
            continue
        url_list = [u for u in map(_text, urls.get(r["id"], [])) if u] or ([_text(r["url"])] if _text(r["url"]) else [])
        parent = r["parent_id"] if names.get(r["parent_id"]) and r["parent_id"] != r["id"] else None
        extra = {k: v for k, v in {
            "aliases": sorted({a for a in map(_text, aliases.get(r["id"], [])) if a}) or None,
            "urls": url_list[1:] or None,
        }.items() if v is not None}
        out.append({"source_id": r["id"], "name": names[r["id"]], "usage": usage(r["id"]), "data": {
            "parent_id": parent,
            "parent": names.get(parent) if parent else None,
            "url": url_list[0] if url_list else None,
            "details": _text(r["details"]),
            "rating": _int(r["rating"], 0, 100),
            "custom_fields": extra,
        }})
    _scale_ratings(out)
    return out
