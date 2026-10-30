"""Folders linked to performers and studios: who may link, and what gets tagged when."""

import os
import shutil
from pathlib import Path

import pytest

from app import folder_links
from app.config import get_settings
from app.scanner import Scanner
from tests.test_user_libraries import drain


async def scan(app, library_id, subpath=None):
    return await Scanner(app.state.pool, get_settings().generated_dir, settle_seconds=0).scan(library_id, subpath)


async def performer(pool, name, owner=None) -> int:
    return await pool.fetchval("INSERT INTO performers (name, owner_id) VALUES ($1, $2) RETURNING id", name, owner)


async def studio(pool, name, owner=None) -> int:
    return await pool.fetchval("INSERT INTO studios (name, owner_id) VALUES ($1, $2) RETURNING id", name, owner)


async def link(c, kind, entity_id, path, scope="new"):
    r = await c.post(f"/api/folder-links/{kind}/{entity_id}", json={"path": str(path), "scope": scope})
    assert r.status_code == 201, r.text
    return r.json()


async def scene_of(pool, path):
    return await pool.fetchrow(
        """SELECT s.id, s.performer_ids, s.studio_id FROM scenes s JOIN files f ON f.id = s.file_id
           WHERE f.path = $1""", str(path))


@pytest.fixture
async def lib(app, clean, client, library_dir):
    """An indexed main library, plus an empty folder to drop files in."""
    pool = app.state.pool
    (library_dir / "inbox" / "deep").mkdir(parents=True)
    lid = await pool.fetchval("INSERT INTO library_paths (path) VALUES ($1) RETURNING id", str(library_dir))
    await scan(app, lid)
    return lid, library_dir


async def test_new_file_gets_every_link_and_only_once(app, client, lib, sample_media):
    lid, root = lib
    pool = app.state.pool
    a, b = await performer(pool, "Ann"), await performer(pool, "Bea")
    st = await studio(pool, "Acme")
    await link(client, "performers", a, root / "inbox")
    await link(client, "performers", b, root / "inbox")
    await link(client, "studios", st, root / "inbox")

    shutil.copy(sample_media / "h264.mp4", root / "inbox" / "new.mp4")
    assert (await scan(app, lid)).tagged == 1
    row = await scene_of(pool, root / "inbox" / "new.mp4")
    assert sorted(row["performer_ids"]) == sorted([a, b]) and row["studio_id"] == st

    # nothing changes on another scan, and one taken off by hand stays off
    await pool.execute("DELETE FROM scene_performers WHERE scene_id = $1 AND performer_id = $2", row["id"], b)
    assert (await scan(app, lid)).tagged == 0
    assert (await scene_of(pool, root / "inbox" / "new.mp4"))["performer_ids"] == [a]


async def test_subfolders_images_and_archives(app, client, lib, sample_media):
    lid, root = lib
    pool = app.state.pool
    a = await performer(pool, "Ann")
    await link(client, "performers", a, root / "inbox")
    shutil.copy(sample_media / "h264.mp4", root / "inbox" / "deep" / "v.mp4")
    shutil.copy(sample_media / "img0.jpg", root / "inbox" / "p.jpg")
    shutil.copy(root / "archive.zip", root / "inbox" / "set.zip")
    await scan(app, lid)
    assert (await scene_of(pool, root / "inbox" / "deep" / "v.mp4"))["performer_ids"] == [a]
    assert await pool.fetchval(
        "SELECT performer_ids FROM images i JOIN files f ON f.id = i.file_id WHERE f.path = $1",
        str(root / "inbox" / "p.jpg")) == [a]
    gallery = await pool.fetchrow(
        "SELECT g.id, g.performer_ids FROM galleries g JOIN files f ON f.id = g.zip_file_id WHERE f.path = $1",
        str(root / "inbox" / "set.zip"))
    assert gallery["performer_ids"] == [a]
    members = await pool.fetch(
        """SELECT i.performer_ids FROM images i JOIN gallery_images gi ON gi.image_id = i.id
           WHERE gi.gallery_id = $1""", gallery["id"])
    assert len(members) == 2 and all(m["performer_ids"] == [a] for m in members)


async def test_a_sibling_with_the_same_prefix_is_not_inside(app, client, lib, sample_media):
    lid, root = lib
    pool = app.state.pool
    (root / "inbox2").mkdir()
    a = await performer(pool, "Ann")
    await link(client, "performers", a, root / "inbox")
    shutil.copy(sample_media / "h264.mp4", root / "inbox2" / "v.mp4")
    await scan(app, lid)
    assert (await scene_of(pool, root / "inbox2" / "v.mp4"))["performer_ids"] == []


async def test_moves_on_disk(app, client, lib):
    lid, root = lib
    pool = app.state.pool
    a = await performer(pool, "Ann")
    await link(client, "performers", a, root / "inbox")

    # into the folder: tagged
    os.rename(root / "videos" / "h264.mp4", root / "inbox" / "h264.mp4")
    assert (await scan(app, lid)).moved == 1
    row = await scene_of(pool, root / "inbox" / "h264.mp4")
    assert row["performer_ids"] == [a]

    # a rename inside it doesn't bring back a performer taken off by hand
    await pool.execute("DELETE FROM scene_performers WHERE scene_id = $1", row["id"])
    os.rename(root / "inbox" / "h264.mp4", root / "inbox" / "deep" / "renamed.mp4")
    await scan(app, lid)
    assert (await scene_of(pool, root / "inbox" / "deep" / "renamed.mp4"))["performer_ids"] == []

    # out of it: what it had stays
    await pool.execute("INSERT INTO scene_performers (scene_id, performer_id) VALUES ($1, $2)", row["id"], a)
    os.rename(root / "inbox" / "deep" / "renamed.mp4", root / "videos" / "back.mp4")
    await scan(app, lid)
    assert (await scene_of(pool, root / "videos" / "back.mp4"))["performer_ids"] == [a]


async def test_move_dialog_tags_too(app, client, lib):
    _, root = lib
    pool = app.state.pool
    a = await performer(pool, "Ann")
    await link(client, "performers", a, root / "inbox")
    row = await scene_of(pool, root / "videos" / "h264.mp4")
    r = await client.post("/api/move", json={"kind": "scenes", "ids": [row["id"]], "dest": str(root / "inbox")})
    assert r.status_code == 200 and r.json()["moved"] == 1, r.text
    assert (await scene_of(pool, root / "inbox" / "h264.mp4"))["performer_ids"] == [a]


async def test_studio_is_never_replaced_and_deepest_wins(app, client, lib, sample_media):
    lid, root = lib
    pool = app.state.pool
    outer, inner, later = [await studio(pool, n) for n in ("Outer", "Inner", "Later")]
    await link(client, "studios", outer, root / "inbox")
    await link(client, "studios", inner, root / "inbox" / "deep")
    await link(client, "studios", later, root / "inbox" / "deep")
    shutil.copy(sample_media / "h264.mp4", root / "inbox" / "deep" / "v.mp4")
    shutil.copy(sample_media / "h265.mkv", root / "inbox" / "w.mkv")
    await scan(app, lid)
    assert (await scene_of(pool, root / "inbox" / "deep" / "v.mp4"))["studio_id"] == inner
    assert (await scene_of(pool, root / "inbox" / "w.mkv"))["studio_id"] == outer

    # the link list warns about the clash
    links = (await client.get(f"/api/folder-links/studios/{outer}")).json()["links"]
    assert links[0]["other_studios"] == 2

    # a studio chosen by hand stays, including through back-tagging
    await pool.execute("UPDATE scenes SET studio_id = $2 WHERE id = $1",
                       (await scene_of(pool, root / "videos" / "h264.mp4"))["id"], later)
    await link(client, "studios", outer, root / "videos", "all")
    await drain(app)
    assert (await scene_of(pool, root / "videos" / "h264.mp4"))["studio_id"] == later
    assert (await scene_of(pool, root / "videos" / "vp9.webm"))["studio_id"] == outer


async def test_new_only_leaves_existing_files(app, client, lib):
    lid, root = lib
    pool = app.state.pool
    a = await performer(pool, "Ann")
    await link(client, "performers", a, root / "videos")
    await scan(app, lid)
    await drain(app)
    assert await pool.fetchval("SELECT count(*) FROM scene_performers") == 0


async def test_all_files_backfills_in_batches_and_can_run_twice(app, client, lib, monkeypatch):
    _, root = lib
    pool = app.state.pool
    monkeypatch.setattr(folder_links, "BATCH", 2)
    a, b = await performer(pool, "Ann"), await performer(pool, "Bea")
    await link(client, "performers", b, root / "videos")            # new files only
    made = await link(client, "performers", a, root / "videos", "all")
    assert made["job_id"]
    await drain(app)
    rows = await pool.fetch("SELECT performer_id FROM scene_performers")
    assert [r["performer_id"] for r in rows] == [a] * 3             # only that link's performer
    assert await pool.fetchval("SELECT message FROM jobs WHERE id = $1", made["job_id"]) == "tagged 3 of 3 files"

    again = await link(client, "performers", a, root / "videos", "all")
    assert again["id"] == made["id"]
    await drain(app)
    assert await pool.fetchval("SELECT count(*) FROM scene_performers") == 3
    assert await pool.fetchval("SELECT count(*) FROM folder_links WHERE performer_id = $1", a) == 1
    assert await pool.fetchval("SELECT message FROM jobs WHERE id = $1", again["job_id"]) == "tagged 0 of 3 files"


async def test_backfill_of_a_removed_link_does_nothing(app, client, lib):
    _, root = lib
    pool = app.state.pool
    a = await performer(pool, "Ann")
    made = await link(client, "performers", a, root / "videos", "all")
    r = await client.delete(f"/api/folder-links/performers/{a}/{made['id']}")
    assert r.status_code == 204
    await drain(app)
    assert await pool.fetchval("SELECT count(*) FROM scene_performers") == 0


async def test_removing_a_link_keeps_tags_and_stops_tagging(app, client, lib, sample_media):
    lid, root = lib
    pool = app.state.pool
    a = await performer(pool, "Ann")
    made = await link(client, "performers", a, root / "inbox")
    shutil.copy(sample_media / "h264.mp4", root / "inbox" / "one.mp4")
    await scan(app, lid)
    assert (await client.delete(f"/api/folder-links/performers/{a}/{made['id']}")).status_code == 204
    assert (await client.delete(f"/api/folder-links/performers/{a}/{made['id']}")).status_code == 404
    shutil.copy(sample_media / "h265.mkv", root / "inbox" / "two.mkv")
    await scan(app, lid)
    assert (await scene_of(pool, root / "inbox" / "one.mp4"))["performer_ids"] == [a]
    assert (await scene_of(pool, root / "inbox" / "two.mkv"))["performer_ids"] == []


async def test_a_deleted_folder_is_reported_and_the_link_kept(app, client, lib):
    _, root = lib
    pool = app.state.pool
    a = await performer(pool, "Ann")
    await link(client, "performers", a, root / "inbox")
    shutil.rmtree(root / "inbox")
    links = (await client.get(f"/api/folder-links/performers/{a}")).json()["links"]
    assert len(links) == 1 and links[0]["missing"] is True


@pytest.fixture
async def people(app, clean, client, login_as, sample_media):
    """alice owns a library shared with bob; eddie is an editor; vic is a viewer."""
    pool = app.state.pool
    alice, bob, eddie, vic = [await login_as(n, r) for n, r in
                              (("alice", "viewer"), ("bob", "viewer"), ("eddie", "editor"), ("vic", "viewer"))]
    await pool.execute("UPDATE users SET can_upload = true WHERE id = ANY($1::bigint[])", [alice.user_id, bob.user_id])
    media = Path(get_settings().media_dir)
    shared, private, main = media / "alice-shared", media / "alice-private", media / "main"
    for d in (shared / "sub", private, main / "sub"):
        d.mkdir(parents=True, exist_ok=True)
    shared_id = await pool.fetchval(
        "INSERT INTO library_paths (path, owner_id, name, visibility) VALUES ($1, $2, 'Shared', 'users') RETURNING id",
        str(shared), alice.user_id)
    await pool.execute("INSERT INTO library_shares (library_id, user_id) VALUES ($1, $2)", shared_id, bob.user_id)
    await pool.execute("INSERT INTO library_paths (path, owner_id, name) VALUES ($1, $2, 'Private')",
                       str(private), alice.user_id)
    main_id = await pool.fetchval("INSERT INTO library_paths (path) VALUES ($1) RETURNING id", str(main))
    yield {"alice": alice, "bob": bob, "eddie": eddie, "vic": vic, "shared": shared, "private": private,
           "main": main, "main_id": main_id, "shared_id": shared_id}
    for d in (shared, private, main):
        shutil.rmtree(d, ignore_errors=True)


async def test_access_is_enforced_on_the_server(app, client, people):
    pool = app.state.pool
    global_p = await performer(pool, "Global")
    bobs = await performer(pool, "Bob's", people["bob"].user_id)
    alices = await performer(pool, "Alice's", people["alice"].user_id)

    async def status(c, kind, entity_id, path):
        return (await c.post(f"/api/folder-links/{kind}/{entity_id}", json={"path": str(path)})).status_code

    # a viewer can't tag the main library, whatever they send
    assert await status(people["vic"], "performers", global_p, people["main"]) == 403
    # a library shared with you can be seen, not tagged
    assert await status(people["bob"], "performers", bobs, people["shared"]) == 403
    # someone else's private library is refused just the same, as is a folder that doesn't exist there
    assert await status(people["bob"], "performers", bobs, people["private"]) == 403
    assert await status(people["bob"], "performers", bobs, people["private"] / "nope") == 403
    # admins get no special access to private libraries
    assert await status(client, "performers", global_p, people["private"]) == 403
    # outside every library, and a way out through ".."
    assert await status(client, "performers", global_p, "/etc") == 403
    assert await status(client, "performers", global_p, f"{people['main']}/../../..") == 403
    assert await status(client, "performers", global_p, "relative/path") == 422
    # someone else's personal performer doesn't exist for you
    assert await status(people["bob"], "performers", alices, people["main"]) == 404

    # allowed: an editor in a main library, an owner in their own, with a global performer
    assert await status(people["eddie"], "performers", global_p, people["main"]) == 201
    assert await status(people["alice"], "performers", global_p, people["shared"] / "sub") == 201
    assert await status(people["alice"], "performers", alices, people["private"]) == 201


async def test_a_link_inside_a_symlink_leaving_the_library_is_refused(app, client, people):
    escape = people["main"] / "escape"
    escape.symlink_to(people["private"])
    st = await studio(app.state.pool, "Acme")
    r = await client.post(f"/api/folder-links/studios/{st}", json={"path": str(escape)})
    assert r.status_code == 403
    r = await client.get("/api/folder-links/folders", params={"path": str(escape)})
    assert r.status_code == 403


async def test_links_and_folders_are_only_shown_to_those_who_can_tag(app, people):
    pool = app.state.pool
    p = await performer(pool, "Global")
    made = await link(people["alice"], "performers", p, people["shared"])
    assert (await people["bob"].get(f"/api/folder-links/performers/{p}")).json()["links"] == []
    assert (await people["vic"].get(f"/api/folder-links/performers/{p}")).json() == {"can_link": False, "links": []}
    names = [e["path"] for e in (await people["bob"].get("/api/folder-links/folders")).json()["entries"]]
    assert str(people["main"]) not in names and str(people["shared"]) not in names
    r = await people["bob"].delete(f"/api/folder-links/performers/{p}/{made['id']}")
    assert r.status_code == 404
    assert await pool.fetchval("SELECT count(*) FROM folder_links") == 1


async def test_a_link_stops_when_its_creator_loses_the_right(app, people, login_as, sample_media):
    pool = app.state.pool
    p = await performer(pool, "Global")
    await link(people["eddie"], "performers", p, people["main"])
    await pool.execute("UPDATE users SET role = 'viewer' WHERE id = $1", people["eddie"].user_id)
    shutil.copy(sample_media / "h264.mp4", people["main"] / "sub" / "v.mp4")
    await scan(app, people["main_id"])
    assert (await scene_of(pool, people["main"] / "sub" / "v.mp4"))["performer_ids"] == []

    # someone who still has the right links it again: it works, for what's there too
    editor2 = await login_as("edna", "editor")
    await link(editor2, "performers", p, people["main"], "all")
    await drain(app)
    assert (await scene_of(pool, people["main"] / "sub" / "v.mp4"))["performer_ids"] == [p]
    assert await pool.fetchval("SELECT count(*) FROM folder_links") == 1
