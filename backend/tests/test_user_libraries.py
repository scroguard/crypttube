"""User-owned libraries: uploads, privacy, sharing, personal metadata, edit rights."""

import os
from pathlib import Path

import pytest

from app import counts, worker
from app.config import get_settings


@pytest.fixture
async def people(app, clean, client, login_as):
    """admin (client), an editor, and three viewers; alice may upload."""
    pool = app.state.pool
    alice, bob, carol = [await login_as(n, "viewer") for n in ("alice", "bob", "carol")]
    editor = await login_as("eddie", "editor")
    await pool.execute("UPDATE users SET can_upload = true WHERE id = $1", alice.user_id)
    return {"admin": client, "alice": alice, "bob": bob, "carol": carol, "editor": editor}


async def drain(app):
    """Run queued jobs (scans, generation) and bring the per-viewer counts up to date."""
    settings = get_settings().model_copy(update={"scan_settle_seconds": 0})
    while await worker.run_one(app.state.pool, settings):
        pass
    await counts.refresh(app.state.pool)


async def upload(c, lib_id, path, data: bytes, **params):
    return await c.put(f"/api/my/libraries/{lib_id}/files", params={"path": path, **params}, content=data)


@pytest.fixture
async def alice_lib(app, people, sample_media):
    """Alice's private library with one video and a folder of two images, indexed."""
    alice = people["alice"]
    r = await alice.post("/api/my/libraries", json={"name": "Alice's stuff"})
    assert r.status_code == 201, r.text
    lib = r.json()
    assert (await upload(alice, lib["id"], "clip.mp4", (sample_media / "h264.mp4").read_bytes())).status_code == 201
    for n in (0, 1):
        r = await upload(alice, lib["id"], f"album/pic{n}.jpg", (sample_media / f"img{n}.jpg").read_bytes())
        assert r.status_code == 201, r.text
    await drain(app)
    scene = (await alice.get("/api/scenes")).json()["items"][0]
    gallery = (await alice.get("/api/galleries")).json()["items"][0]
    return {"id": lib["id"], "scene": scene["id"], "gallery": gallery["id"]}


async def scene_ids(c, **params):
    return [s["id"] for s in (await c.get("/api/scenes", params=params)).json()["items"]]


# --- who may upload ---------------------------------------------------------------

async def test_uploads_must_be_enabled_by_an_admin(app, people):
    bob, admin = people["bob"], people["admin"]
    body = (await bob.get("/api/my/libraries")).json()
    assert {k: body[k] for k in ("can_upload", "quota_bytes", "used_bytes", "libraries")} == {
        "can_upload": False, "quota_bytes": None, "used_bytes": 0, "libraries": []}
    assert (await bob.post("/api/my/libraries", json={"name": "x"})).status_code == 403
    assert (await bob.get("/api/my/share-targets")).status_code == 403

    r = await admin.patch(f"/api/users/{bob.user_id}", json={"can_upload": True, "upload_quota_bytes": 5_000_000})
    assert r.status_code == 200 and r.json()["can_upload"] is True
    info = (await bob.get("/api/my/libraries")).json()
    assert info["can_upload"] is True and info["quota_bytes"] == 5_000_000
    assert (await bob.post("/api/my/libraries", json={"name": "x"})).status_code == 201
    assert (await bob.get("/api/auth/me")).json()["can_upload"] is True
    # quota can be lifted again with an explicit null
    r = await admin.patch(f"/api/users/{bob.user_id}", json={"upload_quota_bytes": None})
    assert r.json()["upload_quota_bytes"] is None


# --- upload pipeline --------------------------------------------------------------

async def test_upload_is_indexed_like_any_library(app, people, alice_lib):
    alice = people["alice"]
    scene = (await alice.get(f"/api/scenes/{alice_lib['scene']}")).json()
    assert scene["title"] == "clip" and scene["video_codec"] == "h264"
    assert scene["can_edit"] is True and scene["mine"] is True
    assert (await alice.get(f"/api/scenes/{alice_lib['scene']}/stream", headers={"Range": "bytes=0-9"})).status_code == 206
    assert (await alice.get(f"/api/scenes/{alice_lib['scene']}/screenshot")).status_code == 200
    gallery = (await alice.get(f"/api/galleries/{alice_lib['gallery']}")).json()
    assert gallery["title"] == "album" and gallery["image_count"] == 2

    info = (await alice.get("/api/my/libraries")).json()
    [lib] = info["libraries"]
    assert (lib["file_count"], lib["scene_count"], lib["image_count"], lib["visibility"]) == (3, 1, 2, "private")
    assert info["used_bytes"] == lib["bytes"] > 0
    files = (await alice.get(f"/api/my/libraries/{alice_lib['id']}/files")).json()
    assert [f["path"] for f in files] == ["album/pic0.jpg", "album/pic1.jpg", "clip.mp4"]
    assert files[2]["scene_id"] == alice_lib["scene"]
    assert await scene_ids(alice, library_ids=alice_lib["id"]) == [alice_lib["scene"]]
    # stored inside the uploads volume, under the owner's folder
    path = await app.state.pool.fetchval("SELECT path FROM library_paths WHERE id = $1", alice_lib["id"])
    assert Path(path).parent == Path(os.environ["UPLOADS_DIR"]) / str(alice.user_id)


async def test_upload_validation(app, people, alice_lib, sample_media):
    alice, lib = people["alice"], alice_lib["id"]
    data = (sample_media / "img0.jpg").read_bytes()
    for bad in ("../escape.jpg", "/abs.jpg", "a/../../b.jpg", ".hidden.jpg", "dir/.x/a.jpg", " lead.jpg"):
        assert (await upload(alice, lib, bad, data)).status_code == 422, bad
    assert (await upload(alice, lib, "notes.txt", b"hello")).status_code == 415
    assert (await upload(alice, lib, "script.sh", b"#!/bin/sh")).status_code == 415
    assert (await upload(alice, lib, "empty.jpg", b"")).status_code == 422
    assert (await upload(alice, lib, "clip.mp4", data)).status_code == 409           # exists
    assert (await upload(alice, lib, "clip.mp4", data, overwrite=True)).status_code == 201
    assert (await upload(people["bob"], lib, "x.jpg", data)).status_code == 403      # can't upload at all
    await app.state.pool.execute("UPDATE users SET can_upload = true WHERE id = $1", people["bob"].user_id)
    assert (await upload(people["bob"], lib, "x.jpg", data)).status_code == 404      # not his library
    root = Path(await app.state.pool.fetchval("SELECT path FROM library_paths WHERE id = $1", lib))
    assert not [p for p in root.rglob(".upload-*")]                                 # no leftovers
    assert not (root.parent.parent / "escape.jpg").exists()


async def test_quota_is_enforced_while_streaming(app, people):
    alice = people["alice"]
    await app.state.pool.execute("UPDATE users SET upload_quota_bytes = 1000 WHERE id = $1", alice.user_id)
    lib = (await alice.post("/api/my/libraries", json={"name": "q"})).json()["id"]
    assert (await upload(alice, lib, "a.jpg", b"x" * 600)).status_code == 201
    r = await upload(alice, lib, "b.jpg", b"x" * 600)
    assert r.status_code == 413 and "quota" in r.text

    async def chunks():  # no Content-Length: must be caught mid-stream
        for _ in range(6):
            yield b"x" * 100
    r = await alice.put(f"/api/my/libraries/{lib}/files", params={"path": "c.jpg"}, content=chunks())
    assert r.status_code == 413
    root = Path(await app.state.pool.fetchval("SELECT path FROM library_paths WHERE id = $1", lib))
    assert sorted(p.name for p in root.iterdir()) == ["a.jpg"]


# --- privacy ----------------------------------------------------------------------

async def test_private_library_is_invisible_to_everyone_else_including_admins(people, alice_lib):
    s, g = alice_lib["scene"], alice_lib["gallery"]
    for who in ("bob", "editor", "admin"):
        c = people[who]
        assert await scene_ids(c) == [], who
        assert (await c.get("/api/galleries")).json()["items"] == [], who
        assert (await c.get("/api/images")).json()["items"] == [], who
        for url in (f"/api/scenes/{s}", f"/api/scenes/{s}/stream", f"/api/scenes/{s}/screenshot",
                    f"/api/scenes/{s}/playback", f"/api/scenes/{s}/sprite.vtt",
                    f"/api/galleries/{g}", f"/api/galleries/{g}/images"):
            assert (await c.get(url)).status_code == 404, (who, url)
        assert (await c.get("/api/scenes", params={"with_total": True})).json()["total"] == 0
        assert await scene_ids(c, library_ids=alice_lib["id"]) == []
        assert (await c.patch(f"/api/scenes/{s}", json={"title": "x"})).status_code == 404
        assert (await c.post("/api/scenes/bulk", json={"ids": [s], "set": {"rating": 1}})).status_code == 404
    image = (await people["alice"].get(f"/api/galleries/{g}/images")).json()["items"][0]["id"]
    for who in ("bob", "admin"):
        assert (await people[who].get(f"/api/images/{image}/thumbnail")).status_code == 404
        assert (await people[who].get(f"/api/images/{image}/file")).status_code == 404


async def test_admin_can_manage_storage_but_not_see_content(app, people, alice_lib):
    admin = people["admin"]
    assert (await admin.get("/api/libraries")).json() == []          # global libraries only
    [summary] = (await admin.get("/api/libraries/user")).json()
    assert summary["owner"]["username"] == "alice" and summary["file_count"] == 3 and summary["bytes"] > 0
    assert set(summary) == {"id", "name", "visibility", "created_at", "owner", "file_count", "bytes"}
    assert (await admin.patch(f"/api/libraries/{alice_lib['id']}", json={"enabled": False})).status_code == 404

    root = Path(await app.state.pool.fetchval("SELECT path FROM library_paths WHERE id = $1", alice_lib["id"]))
    assert (await admin.delete(f"/api/libraries/{alice_lib['id']}")).status_code == 204
    assert not root.exists()
    assert (await people["alice"].get("/api/my/libraries")).json()["libraries"] == []
    assert await app.state.pool.fetchval("SELECT count(*) FROM scenes") == 0


async def test_sharing_with_specific_users_and_everyone(app, people, alice_lib):
    alice, bob, carol, admin = (people[k] for k in ("alice", "bob", "carol", "admin"))
    lib, s = alice_lib["id"], alice_lib["scene"]
    targets = {u["username"] for u in (await alice.get("/api/my/share-targets")).json()}
    assert {"bob", "carol", "eddie"} <= targets and "alice" not in targets

    r = await alice.patch(f"/api/my/libraries/{lib}", json={"visibility": "users", "shared_user_ids": [bob.user_id]})
    assert [u["username"] for u in r.json()["shared_with"]] == ["bob"]
    await counts.refresh(app.state.pool)
    assert await scene_ids(bob) == [s]
    assert (await bob.get(f"/api/scenes/{s}/stream", headers={"Range": "bytes=0-9"})).status_code == 206
    detail = (await bob.get(f"/api/scenes/{s}")).json()
    assert detail["can_edit"] is False and detail["mine"] is False
    assert await scene_ids(carol) == [] and await scene_ids(admin) == []

    # only the owner controls sharing
    assert (await bob.patch(f"/api/my/libraries/{lib}", json={"visibility": "everyone"})).status_code == 404
    assert (await alice.patch(f"/api/my/libraries/{lib}", json={"shared_user_ids": [999999]})).status_code == 422

    await alice.patch(f"/api/my/libraries/{lib}", json={"visibility": "everyone"})
    for c in (bob, carol, admin, people["editor"]):
        assert await scene_ids(c) == [s]

    await alice.patch(f"/api/my/libraries/{lib}", json={"visibility": "private"})
    for c in (bob, carol, admin):
        assert await scene_ids(c) == []
        assert (await c.get(f"/api/scenes/{s}/stream")).status_code == 404
    # the share list is kept but inert while private
    assert (await alice.get("/api/my/libraries")).json()["libraries"][0]["visibility"] == "private"


async def test_admin_deny_rules_still_apply_to_shared_content(app, people, alice_lib):
    alice, bob, admin = people["alice"], people["bob"], people["admin"]
    tag = (await admin.post("/api/tags", json={"name": "Restricted"})).json()["id"]
    assert (await alice.patch(f"/api/scenes/{alice_lib['scene']}", json={"tag_ids": [tag]})).status_code == 200
    await alice.patch(f"/api/my/libraries/{alice_lib['id']}", json={"visibility": "everyone"})
    await admin.put(f"/api/users/{bob.user_id}/restrictions", json={"tag_ids": [tag]})
    await counts.refresh(app.state.pool)
    assert await scene_ids(people["carol"]) == [alice_lib["scene"]]
    assert await scene_ids(bob) == []


# --- edit rights ------------------------------------------------------------------

async def test_only_the_owner_edits_their_content(app, people, alice_lib, library_dir):
    alice, bob, editor, admin = (people[k] for k in ("alice", "bob", "editor", "admin"))
    s = alice_lib["scene"]
    await alice.patch(f"/api/my/libraries/{alice_lib['id']}", json={"visibility": "everyone"})

    r = await alice.patch(f"/api/scenes/{s}", json={"title": "Mine", "rating": 80})   # a viewer, but the owner
    assert r.status_code == 200 and r.json()["title"] == "Mine"
    for c in (bob, editor, admin):   # they can see it, not change it
        assert (await c.get(f"/api/scenes/{s}")).json()["can_edit"] is False
        assert (await c.patch(f"/api/scenes/{s}", json={"title": "hijack"})).status_code == 403
        assert (await c.post("/api/scenes/bulk", json={"ids": [s], "set": {"rating": 1}})).status_code == 403
    assert (await alice.get(f"/api/scenes/{s}")).json()["title"] == "Mine"

    # ...and owning a library doesn't grant rights over global content
    lib = (await admin.post("/api/libraries", json={"path": str(library_dir)})).json()
    await admin.post(f"/api/libraries/{lib['id']}/scan")
    await drain(app)
    global_scene = next(i for i in await scene_ids(alice) if i != s)
    assert (await alice.patch(f"/api/scenes/{global_scene}", json={"title": "x"})).status_code == 403
    assert (await editor.patch(f"/api/scenes/{global_scene}", json={"title": "edited"})).status_code == 200
    # a bulk edit with one item you can't edit is refused whole: nothing changes
    before = (await alice.get(f"/api/scenes/{s}")).json()["rating"]
    r = await alice.post("/api/scenes/bulk", json={"ids": [s, global_scene], "set": {"rating": 5}})
    assert r.status_code == 403
    assert (await alice.get(f"/api/scenes/{s}")).json()["rating"] == before != 5

    # lists say which is which, so "select this page" can tell
    assert {i["id"]: i["can_edit"] for i in (await alice.get("/api/scenes")).json()["items"]} == {
        s: True, **{i: False for i in await scene_ids(alice) if i != s}}
    assert {i["id"]: i["can_edit"] for i in (await editor.get("/api/scenes")).json()["items"]}[global_scene] is True
    images = (await alice.get("/api/images")).json()["items"]
    assert {i["can_edit"] for i in images} == {True, False}
    assert all(i["can_edit"] for i in (await alice.get(f"/api/galleries/{alice_lib['gallery']}/images")).json()["items"])


# --- personal metadata ------------------------------------------------------------

async def test_personal_metadata_is_private_until_content_is_shared(app, people, alice_lib):
    alice, bob, editor, admin = (people[k] for k in ("alice", "bob", "editor", "admin"))
    s = alice_lib["scene"]
    tag = (await alice.post("/api/tags", json={"name": "Secret Tag"})).json()
    perf = (await alice.post("/api/performers", json={"name": "Private Person"})).json()
    studio = (await alice.post("/api/studios", json={"name": "Home Studio"})).json()
    assert tag["personal"] and tag["can_edit"] and perf["personal"] and studio["personal"]
    r = await alice.patch(f"/api/scenes/{s}", json={
        "tag_ids": [tag["id"]], "performer_ids": [perf["id"]], "studio_id": studio["id"]})
    assert r.status_code == 200
    await counts.refresh(app.state.pool)

    for c in (bob, editor, admin):
        for kind, item in (("tags", tag), ("performers", perf), ("studios", studio)):
            assert (await c.get(f"/api/{kind}")).json()["items"] == [], kind
            assert (await c.get(f"/api/{kind}", params={"q": item["name"][:4]})).json()["items"] == []
            assert (await c.get(f"/api/{kind}/{item['id']}")).status_code == 404
            assert (await c.get(f"/api/{kind}/{item['id']}/image")).status_code == 404
            assert (await c.patch(f"/api/{kind}/{item['id']}", json={"name": "x"})).status_code == 404
            assert (await c.delete(f"/api/{kind}/{item['id']}")).status_code == 404
    assert [t["name"] for t in (await alice.get("/api/tags")).json()["items"]] == ["Secret Tag"]
    assert (await alice.get(f"/api/tags/{tag['id']}")).json()["scene_count"] == 1

    # ids can't be used to probe or borrow someone's personal metadata
    await app.state.pool.execute("UPDATE users SET can_upload = true WHERE id = $1", bob.user_id)
    bob_lib = (await bob.post("/api/my/libraries", json={"name": "b"})).json()["id"]
    r = await bob.post("/api/tags", json={"name": "child", "parent_ids": [tag["id"]]})
    assert r.status_code == 422 and "does not exist" in r.text
    assert (await editor.post("/api/performers", json={"name": "P", "tag_ids": [tag["id"]]})).status_code == 422
    assert (await admin.post(f"/api/tags/{tag['id']}/merge", json={"source_ids": [tag["id"]]})).status_code == 404
    assert bob_lib

    # the same name is free for others: no collision, no "name taken" leak
    assert (await bob.post("/api/tags", json={"name": "Secret Tag"})).status_code == 201
    assert (await editor.post("/api/tags", json={"name": "secret tag"})).status_code == 201   # a global one
    assert (await alice.post("/api/tags", json={"name": "SECRET TAG"})).status_code == 409     # her own duplicate

    # sharing the content reveals the metadata on it, read-only
    await alice.patch(f"/api/my/libraries/{alice_lib['id']}", json={"visibility": "users", "shared_user_ids": [bob.user_id]})
    await counts.refresh(app.state.pool)
    names = sorted(t["name"] for t in (await bob.get("/api/tags")).json()["items"])
    assert names == ["Secret Tag", "Secret Tag", "secret tag"]   # alice's (via content), his own, the global one
    seen = (await bob.get(f"/api/tags/{tag['id']}")).json()
    assert seen["can_edit"] is False and seen["personal"] is False and seen["scene_count"] == 1
    assert (await bob.patch(f"/api/tags/{tag['id']}", json={"name": "renamed"})).status_code == 403
    assert (await bob.delete(f"/api/performers/{perf['id']}")).status_code == 403
    assert await scene_ids(bob, tag_ids=tag["id"]) == [s]
    assert (await admin.get(f"/api/tags/{tag['id']}")).status_code == 404   # not shared with the admin


async def test_global_metadata_rules(app, people, alice_lib):
    alice, bob, editor = people["alice"], people["bob"], people["editor"]
    g = (await editor.post("/api/tags", json={"name": "Global"})).json()
    assert g["personal"] is False
    assert (await alice.get(f"/api/tags/{g['id']}")).json()["can_edit"] is False
    assert (await alice.patch(f"/api/tags/{g['id']}", json={"name": "x"})).status_code == 403
    assert (await alice.delete(f"/api/tags/{g['id']}")).status_code == 403
    # users can use global tags on their own content
    assert (await alice.patch(f"/api/scenes/{alice_lib['scene']}", json={"tag_ids": [g["id"]]})).status_code == 200
    # someone with neither the editor role nor upload rights can't create metadata
    assert (await bob.post("/api/tags", json={"name": "nope"})).status_code == 403


async def test_counts_are_per_viewer(app, people, alice_lib):
    alice, bob, editor = people["alice"], people["bob"], people["editor"]
    g = (await editor.post("/api/tags", json={"name": "Shared Vocabulary"})).json()["id"]
    await alice.patch(f"/api/scenes/{alice_lib['scene']}", json={"tag_ids": [g]})
    await counts.refresh(app.state.pool)
    assert (await alice.get(f"/api/tags/{g}")).json()["scene_count"] == 1
    assert (await bob.get(f"/api/tags/{g}")).json()["scene_count"] == 0     # can't see alice's scene
    assert (await people["admin"].get(f"/api/tags/{g}")).json()["scene_count"] == 0


# --- deletion ---------------------------------------------------------------------

async def test_delete_file_and_library(app, people, alice_lib):
    alice, lib = people["alice"], alice_lib["id"]
    pool = app.state.pool
    root = Path(await pool.fetchval("SELECT path FROM library_paths WHERE id = $1", lib))

    assert (await alice.delete(f"/api/my/libraries/{lib}/files", params={"path": "album/pic0.jpg"})).status_code == 204
    assert (await alice.get(f"/api/galleries/{alice_lib['gallery']}")).json()["image_count"] == 1
    assert (await alice.delete(f"/api/my/libraries/{lib}/files", params={"path": "album/pic1.jpg"})).status_code == 204
    assert (await alice.get(f"/api/galleries/{alice_lib['gallery']}")).status_code == 404   # emptied gallery removed
    assert not (root / "album").exists()                                                    # and its folder
    assert (await alice.delete(f"/api/my/libraries/{lib}/files", params={"path": "album/pic1.jpg"})).status_code == 404
    assert (await alice.delete(f"/api/my/libraries/{lib}/files", params={"path": "../../x.jpg"})).status_code == 422
    assert (await people["bob"].delete(f"/api/my/libraries/{lib}/files", params={"path": "clip.mp4"})).status_code == 404

    assert (await people["bob"].delete(f"/api/my/libraries/{lib}")).status_code == 404
    assert (await alice.delete(f"/api/my/libraries/{lib}")).status_code == 204
    assert not root.exists()
    assert await pool.fetchval("SELECT count(*) FROM files") == 0


async def test_deleting_a_user_removes_their_uploads_and_metadata(app, people, alice_lib):
    alice, admin = people["alice"], people["admin"]
    await alice.post("/api/tags", json={"name": "Hers"})
    user_dir = Path(os.environ["UPLOADS_DIR"]) / str(alice.user_id)
    assert user_dir.is_dir()
    assert (await admin.delete(f"/api/users/{alice.user_id}")).status_code == 204
    assert not user_dir.exists()
    pool = app.state.pool
    assert await pool.fetchval("SELECT count(*) FROM library_paths") == 0
    assert await pool.fetchval("SELECT count(*) FROM scenes") == 0
    assert await pool.fetchval("SELECT count(*) FROM tags") == 0


async def test_paths_are_not_exposed_to_non_owners(app, people, alice_lib):
    alice, bob, admin = people["alice"], people["bob"], people["admin"]
    await alice.patch(f"/api/my/libraries/{alice_lib['id']}", json={"visibility": "everyone"})
    mine = (await alice.get(f"/api/scenes/{alice_lib['scene']}")).json()
    assert mine["file"]["path"].endswith("/clip.mp4")
    for c in (bob, admin):
        theirs = (await c.get(f"/api/scenes/{alice_lib['scene']}")).json()
        assert "path" not in theirs["file"] and theirs["file"]["size"] > 0
        assert (await c.get(f"/api/galleries/{alice_lib['gallery']}")).json()["folder_path"] is None
    # the admin's job list doesn't reveal folder names inside user libraries
    jobs = (await admin.get("/api/jobs", params={"limit": 50})).json()["items"]
    paths = [j["payload"].get("path") for j in jobs if j["kind"] == "scan"]
    assert "(user library)" in paths and not any(p and "album" in p for p in paths)
    # nor do messages that mention such a folder
    uploads = str(get_settings().uploads_dir)
    await app.state.pool.execute(
        "INSERT INTO jobs (kind, payload, status, error) VALUES ('scan', '{}', 'failed', $1)",
        f"cannot read {uploads}/7/album/x.mp4")
    jobs = (await admin.get("/api/jobs", params={"limit": 50})).json()["items"]
    assert not any("album" in (j["error"] or "") for j in jobs)


async def test_identical_upload_never_adopts_another_owners_missing_file(app, people, library_dir, sample_media):
    """Move detection pairs new files with missing ones by content hash. Across owners
    that would drag a scene and its metadata over the privacy boundary."""
    alice, admin, pool = people["alice"], people["admin"], app.state.pool
    lib = (await admin.post("/api/libraries", json={"path": str(library_dir)})).json()
    await admin.post(f"/api/libraries/{lib['id']}/scan")
    await drain(app)
    global_scene = await pool.fetchval("SELECT id FROM scenes WHERE video_codec = 'h264'")
    await admin.patch(f"/api/scenes/{global_scene}", json={"title": "Global original", "details": "admin notes"})
    (library_dir / "videos" / "h264.mp4").unlink()               # now missing in the global library
    await admin.post(f"/api/libraries/{lib['id']}/scan")
    await drain(app)
    assert await pool.fetchval("SELECT count(*) FROM files WHERE missing_since IS NOT NULL") == 1

    mine = (await alice.post("/api/my/libraries", json={"name": "copy"})).json()["id"]
    assert (await upload(alice, mine, "same-bytes.mp4", (sample_media / "h264.mp4").read_bytes())).status_code == 201
    await drain(app)

    row = await pool.fetchrow("SELECT library_id, title, details FROM scenes WHERE id = $1", global_scene)
    assert (row["library_id"], row["title"]) == (lib["id"], "Global original")     # untouched, still global
    [hers] = (await alice.get("/api/scenes", params={"library_ids": mine})).json()["items"]
    assert hers["id"] != global_scene and hers["title"] == "same-bytes"
    assert (await alice.get(f"/api/scenes/{hers['id']}")).json()["details"] is None

    # ...and the same in the other direction: her missing file isn't adopted by a global scan
    await alice.patch(f"/api/scenes/{hers['id']}", json={"details": "private notes"})
    path = Path(await pool.fetchval("SELECT path FROM library_paths WHERE id = $1", mine)) / "same-bytes.mp4"
    path.unlink()
    await pool.execute("UPDATE files SET missing_since = now() WHERE path = $1", str(path))
    import shutil
    shutil.copy(sample_media / "h264.mp4", library_dir / "videos" / "again.mp4")
    await admin.post(f"/api/libraries/{lib['id']}/scan")
    await drain(app)
    assert await pool.fetchval("SELECT library_id FROM scenes WHERE id = $1", hers["id"]) == mine
    assert await pool.fetchval("SELECT count(*) FROM scenes WHERE details = 'private notes' AND library_id = $1", lib["id"]) == 0


async def test_unindexable_uploads_are_listed_and_deletable(app, people, alice_lib):
    """A corrupt file with a media extension is accepted but can't be probed; it must
    not become an invisible file that eats quota forever."""
    alice, lib = people["alice"], alice_lib["id"]
    assert (await upload(alice, lib, "broken.mp4", b"definitely not a video" * 50)).status_code == 201
    await drain(app)
    files = {f["path"]: f for f in (await alice.get(f"/api/my/libraries/{lib}/files")).json()}
    assert files["broken.mp4"]["indexed"] is False and files["broken.mp4"]["scene_id"] is None
    assert files["clip.mp4"]["indexed"] is True
    assert len(await scene_ids(alice)) == 1                      # the broken one produced no scene
    assert (await alice.delete(f"/api/my/libraries/{lib}/files", params={"path": "broken.mp4"})).status_code == 204
    assert "broken.mp4" not in [f["path"] for f in (await alice.get(f"/api/my/libraries/{lib}/files")).json()]


async def test_rebuilding_a_user_library(app, people, alice_lib):
    from app.jobs import JobContext
    alice, bob, admin, pool = people["alice"], people["bob"], people["admin"], app.state.pool
    lib = alice_lib["id"]
    assert (await bob.post(f"/api/my/libraries/{lib}/regenerate")).status_code == 404
    assert (await admin.post(f"/api/scenes/{alice_lib['scene']}/regenerate")).status_code == 404   # can't even see it
    assert (await alice.post(f"/api/scenes/{alice_lib['scene']}/regenerate")).status_code == 202   # viewer, but owner
    assert (await alice.post(f"/api/my/libraries/{lib}/regenerate")).status_code == 202

    # a failure in a private library is reported without its file name
    root = Path(await pool.fetchval("SELECT path FROM library_paths WHERE id = $1", lib))
    (root / "clip.mp4").write_bytes(b"broken after indexing" * 20)
    payload = {"force": True, "library_id": lib}
    job = await pool.fetchval("INSERT INTO jobs (kind, payload, status) VALUES ('generate', $1, 'running') RETURNING id", payload)
    msg = await worker.handle_generate(pool, get_settings(), JobContext(pool, job), payload)
    assert msg.startswith("0 rebuilt, 1 failed") and f"scene {alice_lib['scene']}:" in msg
    assert "clip" not in msg


async def test_select_all_ids_follow_ownership(app, people, alice_lib, library_dir):
    alice, editor, admin = people["alice"], people["editor"], people["admin"]
    lib = (await admin.post("/api/libraries", json={"path": str(library_dir)})).json()
    await admin.post(f"/api/libraries/{lib['id']}/scan")
    await drain(app)
    await alice.patch(f"/api/my/libraries/{alice_lib['id']}", json={"visibility": "everyone"})
    # alice (a viewer) may edit only her own upload, though she can see the global scenes
    assert (await alice.get("/api/ids/scenes")).json()["ids"] == [alice_lib["scene"]]
    # the editor sees alice's shared scene but may not edit it: it is not selected
    theirs = (await editor.get("/api/ids/scenes")).json()["ids"]
    assert alice_lib["scene"] not in theirs and len(theirs) == 3
    # path search works inside user libraries for those who can see them
    assert await scene_ids(alice, q="clip") == [alice_lib["scene"]]


async def test_upload_types_come_from_the_server(app, people, sample_media):
    """The upload page checks file types against this list; it must include everything
    the scanner indexes (HEIC photos were once refused by a stale copy in the page)."""
    from app import media
    alice = people["alice"]
    exts = set((await alice.get("/api/my/libraries")).json()["upload_extensions"])
    assert {"heic", "heif", "mov", "mp4", "jpg", "zip"} <= exts
    assert exts == {e.lstrip(".") for e in media.VIDEO_EXTS | media.IMAGE_EXTS | media.ARCHIVE_EXTS}
    import pillow_heif
    from PIL import Image
    import io
    buf = io.BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", (64, 48), "blue")).save(buf, format="HEIF")
    lib = (await alice.post("/api/my/libraries", json={"name": "Phone"})).json()["id"]
    assert (await upload(alice, lib, "IMG_0001.HEIC", buf.getvalue())).status_code == 201
    await drain(app)
    items = (await alice.get("/api/images")).json()["items"]
    assert [(i["width"], i["height"]) for i in items] == [(64, 48)]
