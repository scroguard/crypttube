"""Importing tags, performers and studios from a Stash database, with admin review."""

import os
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from app.stash_import import StashError, read_stash

# The relevant part of Stash's schema (version 85).
SCHEMA = """
CREATE TABLE schema_migrations (version uint64, dirty bool);
INSERT INTO schema_migrations VALUES (85, 0);
CREATE TABLE tags (id integer PRIMARY KEY AUTOINCREMENT, name varchar(255), description text,
    sort_name varchar(255), favorite boolean, ignore_auto_tag boolean, image_blob varchar(255));
CREATE TABLE tag_aliases (tag_id integer, alias varchar(255));
CREATE TABLE tags_relations (parent_id integer, child_id integer);
CREATE TABLE performers (id integer PRIMARY KEY AUTOINCREMENT, name varchar(255), disambiguation varchar(255),
    gender varchar(20), birthdate date, ethnicity varchar(255), country varchar(255), eye_color varchar(255),
    height int, measurements varchar(255), fake_tits varchar(255), tattoos varchar(255), piercings varchar(255),
    favorite boolean, details text, death_date date, hair_color varchar(255), weight integer, rating tinyint,
    penis_length float, circumcised varchar(10), career_start date, career_end date, image_blob varchar(255));
CREATE TABLE performer_aliases (performer_id integer, alias varchar(255));
CREATE TABLE performer_urls (performer_id integer, position integer, url varchar(255));
CREATE TABLE performers_tags (performer_id integer, tag_id integer);
CREATE TABLE performers_scenes (performer_id integer, scene_id integer);
CREATE TABLE studios (id integer PRIMARY KEY AUTOINCREMENT, name varchar(255), parent_id integer, details text,
    rating tinyint, favorite boolean, image_blob varchar(255));
CREATE TABLE studio_aliases (studio_id integer, alias varchar(255));
CREATE TABLE studio_urls (studio_id integer, position integer, url varchar(255));
CREATE TABLE scenes (id integer PRIMARY KEY AUTOINCREMENT, title varchar(255), studio_id integer);
CREATE TABLE scenes_tags (scene_id integer, tag_id integer);
"""

DATA = """
INSERT INTO tags (id, name, description) VALUES
    (1, 'Location', 'Where it happens'), (2, 'Outdoor', NULL), (3, 'Beach', NULL), (4, 'Blonde', NULL), (5, '  ', NULL);
INSERT INTO tag_aliases VALUES (2, 'Outside'), (2, 'Open air');
INSERT INTO tags_relations VALUES (1, 2), (2, 3);
INSERT INTO scenes (id, title, studio_id) VALUES (1, 'a', 2), (2, 'b', 2), (3, 'c', 1);
INSERT INTO scenes_tags VALUES (1, 3), (2, 3), (1, 2);
INSERT INTO performers (id, name, disambiguation, gender, birthdate, death_date, ethnicity, country, eye_color,
        hair_color, height, weight, measurements, fake_tits, tattoos, favorite, details, rating, career_start, career_end)
    VALUES (1, 'Jane Roe', NULL, 'FEMALE', '1990-05-17', NULL, 'Caucasian', 'US', 'Blue',
            'Blonde', 168, 55, '34B-24-34', 'No', 'Left ankle', 1, 'Bio text', 80, '2010-01-01', '2020-06-01'),
           (2, 'Jane Roe', 'the other one', 'TRANSGENDER_FEMALE', 'not a date', NULL, NULL, NULL, NULL,
            NULL, 0, NULL, NULL, NULL, NULL, 0, NULL, NULL, NULL, NULL),
           (3, 'John Doe', NULL, 'MALE', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, NULL, NULL, NULL);
INSERT INTO performer_aliases VALUES (1, 'JR'), (1, 'Janey');
INSERT INTO performer_urls VALUES (1, 0, 'https://example.com/jane');
INSERT INTO performers_tags VALUES (1, 4), (1, 3);
INSERT INTO performers_scenes VALUES (1, 1), (1, 2), (3, 1);
INSERT INTO studios (id, name, parent_id, details, rating) VALUES
    (1, 'Big Network', NULL, 'The parent', 60), (2, 'Small Label', 1, NULL, NULL);
INSERT INTO studio_aliases VALUES (2, 'SL');
INSERT INTO studio_urls VALUES (2, 1, 'https://second.example'), (2, 0, 'https://small.example');
"""


def make_stash(path: Path, schema: str = SCHEMA, data: str = DATA) -> Path:
    con = sqlite3.connect(path)
    con.executescript(schema + data)
    con.commit()
    con.close()
    return path


@pytest.fixture
def stash_db(tmp_path) -> Path:
    return make_stash(tmp_path / "stash-go.sqlite")


# --- the reader --------------------------------------------------------------------------

def test_reader_maps_stash_fields(stash_db):
    before = stash_db.read_bytes()
    s = read_stash(str(stash_db))
    assert s["version"] == 85

    tags = {t["name"]: t for t in s["tags"]}
    assert set(tags) == {"Location", "Outdoor", "Beach", "Blonde"}        # the blank name is dropped
    assert tags["Outdoor"]["data"]["parents"] == ["Location"]
    assert tags["Outdoor"]["data"]["aliases"] == ["Open air", "Outside"]
    assert tags["Beach"]["usage"] == 3                                    # 2 scenes + 1 performer

    jane, other, john = s["performers"]
    assert jane["usage"] == 2
    assert jane["data"] | {"custom_fields": None} == {
        "disambiguation": None, "gender": "female", "birthdate": "1990-05-17", "deathdate": None,
        "ethnicity": "Caucasian", "country": "US", "eye_color": "Blue", "hair_color": "Blonde",
        "height_cm": 168, "weight_kg": 55, "measurements": "34B-24-34", "tattoos": "Left ankle",
        "piercings": None, "career_start": 2010, "career_end": 2020, "details": "Bio text", "rating": 80,
        "favorite": True, "aliases": ["JR", "Janey"], "custom_fields": None,
        "tag_ids": [4, 3], "tags": ["Blonde", "Beach"],
    }
    assert jane["data"]["custom_fields"] == {"urls": ["https://example.com/jane"], "fake_tits": "No"}
    # junk values become "unknown" rather than failing the import
    assert other["data"]["gender"] == "transgender_female"
    assert other["data"]["birthdate"] is None and other["data"]["height_cm"] is None
    assert john["data"]["gender"] == "male"

    big, small = s["studios"]
    assert big["usage"] == 1 and small["usage"] == 2
    assert small["data"]["parent"] == "Big Network"
    assert small["data"]["url"] == "https://small.example"               # position 0 first
    assert small["data"]["custom_fields"] == {"aliases": ["SL"], "urls": ["https://second.example"]}

    # strictly read-only: the file is untouched and no journal/WAL files appear beside it
    assert stash_db.read_bytes() == before
    assert sorted(p.name for p in stash_db.parent.iterdir()) == ["stash-go.sqlite"]


def test_reader_copes_with_old_schemas(tmp_path):
    """Early Stash versions: no alias/url/relation tables, 1-5 ratings, career_length text."""
    db = make_stash(tmp_path / "old.sqlite", schema="""
        CREATE TABLE tags (id integer PRIMARY KEY, name varchar(255));
        CREATE TABLE performers (id integer PRIMARY KEY, name varchar(255), aliases varchar(255), url varchar(255),
            gender varchar(20), height varchar(255), career_length varchar(255), rating tinyint, favorite boolean);
        CREATE TABLE studios (id integer PRIMARY KEY, name varchar(255), url varchar(255), rating tinyint);
    """, data="""
        INSERT INTO tags VALUES (1, 'Old tag');
        INSERT INTO performers VALUES (1, 'Old Star', 'OS, Starry', 'https://old.example', 'FEMALE', '170', '2001 - 2009', 4, 0);
        INSERT INTO studios VALUES (1, 'Old Studio', 'https://studio.example', 5);
    """)
    s = read_stash(str(db))
    assert s["version"] is None
    p = s["performers"][0]["data"]
    assert p["aliases"] == ["OS", "Starry"] and p["height_cm"] == 170
    assert (p["career_start"], p["career_end"], p["rating"]) == (2001, 2009, 80)
    assert p["custom_fields"] == {"urls": ["https://old.example"]}
    assert s["studios"][0]["data"]["url"] == "https://studio.example" and s["studios"][0]["data"]["rating"] == 100


def test_reader_rejects_other_files(tmp_path):
    (tmp_path / "x.sqlite").write_bytes(b"definitely not sqlite")
    with pytest.raises(StashError, match="not an SQLite database"):
        read_stash(str(tmp_path / "x.sqlite"))
    other = make_stash(tmp_path / "other.sqlite", schema="CREATE TABLE notes (id integer);", data="")
    with pytest.raises(StashError, match="doesn't look like a Stash database"):
        read_stash(str(other))


# --- the review workflow ------------------------------------------------------------------

async def upload(client, db: Path):
    return await client.post("/api/stash-import/upload", content=db.read_bytes(),
                             headers={"X-Filename": "stash-go.sqlite"})


async def names(app, table: str) -> list[str]:
    return [r["name"] for r in await app.state.pool.fetch(f"SELECT name FROM {table} ORDER BY name")]


async def test_staging_changes_nothing_until_approved(client, clean, app, stash_db):
    assert (await client.get("/api/stash-import")).json() is None
    r = await upload(client, stash_db)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["filename"] == "stash-go.sqlite" and body["stash_version"] == 85
    assert body["kinds"]["tag"] == {"total": 4, "imported": 0, "existing": 0, "pending": 4}
    assert body["kinds"]["performer"]["total"] == 3 and body["kinds"]["studio"]["total"] == 2
    for table in ("tags", "performers", "studios"):
        assert await names(app, table) == []
    # the uploaded file is not kept
    assert not list((Path(os.environ["GENERATED_DIR"]) / "tmp").iterdir())

    # one review at a time
    assert (await upload(client, stash_db)).status_code == 409

    # rejecting discards everything
    assert (await client.delete("/api/stash-import")).status_code == 204
    assert (await client.get("/api/stash-import")).json() is None
    assert (await client.get("/api/stash-import/items", params={"kind": "tag"})).status_code == 404
    for table in ("tags", "performers", "studios"):
        assert await names(app, table) == []


async def test_selective_then_full_import(client, clean, app, stash_db):
    pool = app.state.pool
    # "Location" already exists here (different case, own description): it must be matched, not overwritten.
    existing = (await client.post("/api/tags", json={"name": "location", "description": "mine"})).json()["id"]
    await upload(client, stash_db)

    listed = (await client.get("/api/stash-import/items", params={"kind": "tag"})).json()
    assert [(t["name"], t["status"]) for t in listed] == [
        ("Beach", "pending"), ("Blonde", "pending"), ("Location", "existing"), ("Outdoor", "pending")]
    assert listed[2]["target_id"] == existing
    outdoor = listed[3]
    assert outdoor["data"]["parents"] == ["Location"] and outdoor["usage"] == 1
    assert outdoor["data"]["description"] == "Also known as: Open air, Outside"

    # --- tags: only Outdoor and Beach ---
    ids = [t["source_id"] for t in listed if t["name"] in ("Outdoor", "Beach")]
    r = await client.post("/api/stash-import/apply", json={"kind": "tag", "source_ids": ids})
    assert r.status_code == 200, r.text
    assert (r.json()["created"], r.json()["existing"]) == (2, 0)
    assert r.json()["import"]["kinds"]["tag"] == {"total": 4, "imported": 2, "existing": 1, "pending": 1}
    assert sorted(await names(app, "tags"), key=str.lower) == ["Beach", "location", "Outdoor"]
    rel = await pool.fetch(
        """SELECT p.name AS parent, c.name AS child FROM tag_relations r
           JOIN tags p ON p.id = r.parent_id JOIN tags c ON c.id = r.child_id ORDER BY 1, 2""")
    # Outdoor hangs under the tag that was already here; Beach under Outdoor
    assert {tuple(x) for x in rel} == {("location", "Outdoor"), ("Outdoor", "Beach")}
    assert await pool.fetchval("SELECT description FROM tags WHERE id = $1", existing) == "mine"

    # --- performers: everything ---
    r = await client.post("/api/stash-import/apply", json={"kind": "performer"})
    assert (r.json()["created"], r.json()["existing"]) == (3, 0)
    jane = await pool.fetchrow("SELECT * FROM performers WHERE name = 'Jane Roe' AND disambiguation IS NULL")
    assert (jane["gender"], str(jane["birthdate"]), jane["height_cm"], jane["weight_kg"], jane["rating"]) == \
        ("female", "1990-05-17", 168, 55, 80)
    assert (jane["career_start"], jane["career_end"], jane["owner_id"], jane["image_path"]) == (2010, 2020, None, None)
    assert [r["alias"] for r in await pool.fetch(
        "SELECT alias FROM performer_aliases WHERE performer_id = $1 ORDER BY 1", jane["id"])] == ["JR", "Janey"]
    # linked to the tag that was imported; "Blonde" was not approved, so it isn't there
    assert [r["name"] for r in await pool.fetch(
        "SELECT t.name FROM performer_tags pt JOIN tags t ON t.id = pt.tag_id WHERE performer_id = $1",
        jane["id"])] == ["Beach"]
    # Stash's favourite becomes the importing admin's favourite
    assert await pool.fetchval("SELECT performer_id FROM user_favorite_performers WHERE user_id = $1",
                               client.user_id) == jane["id"]
    page = (await client.get(f"/api/performers/{jane['id']}")).json()
    assert page["name"] == "Jane Roe" and page["favorite"] is True

    # approving Blonde later links it to the performer imported earlier
    blonde = [t["source_id"] for t in listed if t["name"] == "Blonde"]
    await client.post("/api/stash-import/apply", json={"kind": "tag", "source_ids": blonde})
    assert await pool.fetchval("SELECT count(*) FROM performer_tags WHERE performer_id = $1", jane["id"]) == 2

    # --- studios: child first, then the parent; the link appears once both exist ---
    studios = (await client.get("/api/stash-import/items", params={"kind": "studio"})).json()
    small = next(s for s in studios if s["name"] == "Small Label")
    assert small["data"]["parent"] == "Big Network" and small["data"]["url"] == "https://small.example"
    await client.post("/api/stash-import/apply", json={"kind": "studio", "source_ids": [small["source_id"]]})
    assert await pool.fetchval("SELECT parent_id FROM studios WHERE name = 'Small Label'") is None
    await client.post("/api/stash-import/apply", json={"kind": "studio"})
    row = await pool.fetchrow(
        "SELECT s.url, s.custom_fields, p.name AS parent, p.rating FROM studios s JOIN studios p ON p.id = s.parent_id "
        "WHERE s.name = 'Small Label'")
    assert (row["url"], row["parent"], row["rating"]) == ("https://small.example", "Big Network", 60)

    # applying again is a no-op, and everything is accounted for
    r = await client.post("/api/stash-import/apply", json={"kind": "performer"})
    assert (r.json()["created"], r.json()["existing"]) == (0, 0)
    kinds = r.json()["import"]["kinds"]
    assert kinds["performer"] == {"total": 3, "imported": 3, "existing": 0, "pending": 0}
    assert kinds["studio"]["pending"] == 0 and kinds["tag"]["pending"] == 0
    statuses = {t["name"]: t["status"] for t in (await client.get("/api/stash-import/items", params={"kind": "tag"})).json()}
    assert statuses == {"Beach": "imported", "Blonde": "imported", "Location": "existing", "Outdoor": "imported"}

    # finishing drops the staging but keeps what was imported
    await client.delete("/api/stash-import")
    assert len(await names(app, "performers")) == 3 and len(await names(app, "tags")) == 4


async def test_import_from_a_file_in_the_media_store(client, clean, tmp_path):
    media = Path(os.environ["MEDIA_DIR"])
    db = make_stash(media / "stash-copy.sqlite")
    try:
        r = await client.post("/api/stash-import/path", json={"path": str(db)})
        assert r.status_code == 201, r.text
        assert r.json()["kinds"]["studio"]["total"] == 2 and db.exists()
        await client.delete("/api/stash-import")
        outside = make_stash(tmp_path / "outside.sqlite")
        assert (await client.post("/api/stash-import/path", json={"path": str(outside)})).status_code == 422
        (media / "notes.txt").write_text("hello")
        assert (await client.post("/api/stash-import/path", json={"path": str(media / "notes.txt")})).status_code == 422
        assert (await client.post("/api/stash-import/path", json={"path": str(media / "nope.sqlite")})).status_code == 404
    finally:
        db.unlink(missing_ok=True)
        (media / "notes.txt").unlink(missing_ok=True)


async def test_bad_upload_is_refused(client, clean):
    r = await client.post("/api/stash-import/upload", content=b"not a database")
    assert r.status_code == 422 and "SQLite" in r.json()["detail"]
    assert (await client.get("/api/stash-import")).json() is None


async def test_admins_only(client, clean, login_as, anon, stash_db):
    await upload(client, stash_db)
    for c in (await login_as("ed", "editor"), await login_as("vera", "viewer")):
        assert (await c.get("/api/stash-import")).status_code == 403
        assert (await c.get("/api/stash-import/items", params={"kind": "tag"})).status_code == 403
        assert (await c.post("/api/stash-import/apply", json={"kind": "tag"})).status_code == 403
        assert (await c.delete("/api/stash-import")).status_code == 403
        assert (await c.post("/api/stash-import/images", json={"path": "/media"})).status_code == 403
    assert (await anon.get("/api/stash-import")).status_code == 401
    assert (await client.get("/api/stash-import")).json()["kinds"]["tag"]["pending"] == 4


async def test_images_matched_by_file_name(client, clean, app, tmp_path):
    pool = app.state.pool
    ids = {}
    for name in ("Big Network", "Small-Label!", "No Picture"):
        ids[name] = (await client.post("/api/studios", json={"name": name})).json()["id"]
    folder = Path(os.environ["MEDIA_DIR"]) / "_studio-images"
    folder.mkdir()
    try:
        Image.new("RGB", (400, 300), "red").save(folder / "big network.png")
        Image.new("RGB", (400, 300), "blue").save(folder / "Small Label.jpg")
        Image.new("RGB", (40, 30), "green").save(folder / "Somebody Else.jpg")
        (folder / "Big Network.txt").write_text("not an image")

        r = await client.post("/api/stash-import/images", json={"kind": "studios", "path": str(folder)})
        assert r.status_code == 200, r.text
        assert r.json() == {"files": 3, "matched": 2, "skipped": 0, "failed": 0, "unmatched": ["Somebody Else.jpg"], "unmatched_total": 1}
        paths = {r["name"]: r["image_path"] for r in await pool.fetch("SELECT name, image_path FROM studios")}
        assert paths == {"Big Network": f"entities/studios/{ids['Big Network']}.webp",
                         "Small-Label!": f"entities/studios/{ids['Small-Label!']}.webp", "No Picture": None}
        stored = Path(os.environ["GENERATED_DIR"]) / paths["Big Network"]
        with Image.open(stored) as im:
            assert im.format == "WEBP" and im.size == (400, 300)
        # the source folder is only read
        assert sorted(p.name for p in folder.iterdir()) == [
            "Big Network.txt", "Small Label.jpg", "Somebody Else.jpg", "big network.png"]

        # existing pictures are kept unless overwriting is asked for
        Image.new("RGB", (200, 100), "white").save(folder / "big network.png")
        again = (await client.post("/api/stash-import/images", json={"path": str(folder)})).json()
        assert (again["matched"], again["skipped"]) == (0, 2) and again["unmatched"] == ["Somebody Else.jpg"]
        with Image.open(stored) as im:
            assert im.size == (400, 300)
        over = (await client.post("/api/stash-import/images", json={"path": str(folder), "overwrite": True})).json()
        assert over["matched"] == 2
        with Image.open(stored) as im:
            assert im.size == (200, 100)

        assert (await client.post("/api/stash-import/images", json={"path": str(tmp_path)})).status_code == 422
        assert (await client.post("/api/stash-import/images", json={"path": str(folder / "nope")})).status_code == 404
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
