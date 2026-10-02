"""Admins deleting media for good: file, database entries, generated content."""

import errno
import os
import shutil

from app import deletion, generated, worker
from app.config import get_settings
from app.jobs import JobContext
from app.scanner import Scanner
from tests.test_media_api import indexed  # noqa: F401  (fixture)
from tests.test_user_libraries import alice_lib, people  # noqa: F401  (fixtures)


def gen_files() -> set[str]:
    root = get_settings().generated_dir
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


async def rescan(app):
    for r in await app.state.pool.fetch("SELECT id FROM library_paths WHERE owner_id IS NULL"):
        await Scanner(app.state.pool, get_settings().generated_dir, settle_seconds=0).scan(r["id"])


async def post(client, kind, ids):
    return await client.post("/api/delete", json={"kind": kind, "ids": ids})


async def test_delete_scene_image_and_galleries(app, client, indexed):
    pool = app.state.pool
    root = indexed["root"]
    sid = indexed["scenes"]["h264"]
    assert (await client.get(f"/api/scenes/{sid}/screenshot", params={"w": 320})).status_code == 200
    plain = next(i for p, i in indexed["images"].items() if p.endswith("pics/img10.jpg"))
    zipped = indexed["images"]["a.jpg"]
    for iid in indexed["images"].values():
        assert (await client.get(f"/api/images/{iid}/thumbnail")).status_code == 200
    before = gen_files()

    r = await post(client, "scenes", [sid, 999999])
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1 and r.json()["skipped"] == [{"id": 999999, "reason": "not found"}]
    assert r.json()["bytes"] > 0
    assert not (root / "videos" / "h264.mp4").exists() and (root / "videos" / "vp9.webm").exists()
    assert (await client.get(f"/api/scenes/{sid}")).status_code == 404
    assert await pool.fetchval("SELECT count(*) FROM files WHERE path LIKE '%h264.mp4'") == 0
    assert len(before - gen_files()) == 2            # its screenshot and the thumbnail made from it

    # a single image; one inside an archive can't be deleted on its own
    r = await post(client, "images", [plain, zipped])
    assert r.json()["deleted"] == 1
    assert r.json()["skipped"] == [{"id": zipped, "reason": "it is inside an archive; delete the gallery instead"}]
    assert not (root / "pics" / "img10.jpg").exists() and (root / "archive.zip").exists()
    assert len(before - gen_files()) == 3

    # galleries: the archive, and a folder gallery with every image in it
    galleries = {("zip" if g["zip_file_id"] else "folder"): g["id"]
                 for g in await pool.fetch("SELECT id, zip_file_id FROM galleries")}
    r = await post(client, "galleries", list(galleries.values()))
    assert r.json()["deleted"] == 2 and r.json()["skipped"] == []
    assert not (root / "archive.zip").exists() and not any((root / "pics").glob("*.jpg"))
    assert await pool.fetchval("SELECT count(*) FROM galleries") == 0
    assert await pool.fetchval("SELECT count(*) FROM images") == 0
    assert not [f for f in gen_files() if f.startswith("thumbs/images")]
    # nothing else was touched
    assert sorted(p.name for p in (root / "videos").iterdir()) == ["h265.mkv", "vp9.webm"]
    assert await pool.fetchval("SELECT count(*) FROM scenes") == 2


async def test_read_only_store_changes_nothing(app, client, indexed, monkeypatch):
    def refuse(path):
        raise OSError(errno.EROFS, "Read-only file system")
    monkeypatch.setattr(deletion.os, "unlink", refuse)
    sid = indexed["scenes"]["h264"]
    r = await post(client, "scenes", [sid])
    assert r.status_code == 409 and "MEDIA_MODE=rw" in r.json()["detail"]
    assert (await client.get(f"/api/scenes/{sid}")).status_code == 200
    assert (indexed["root"] / "videos" / "h264.mp4").exists()


async def test_shared_previews_survive_while_a_copy_remains(app, client, indexed):
    root = indexed["root"]
    shutil.copy(root / "videos" / "h264.mp4", root / "videos" / "copy.mp4")
    await rescan(app)
    a, b = [r["id"] for r in await app.state.pool.fetch(
        "SELECT s.id FROM scenes s JOIN files f ON f.id = s.file_id WHERE f.path ~ '(h264|copy).mp4$' ORDER BY f.path")]
    assert (await client.get(f"/api/scenes/{a}/screenshot")).status_code == 200
    before = gen_files()
    assert (await post(client, "scenes", [a])).json()["deleted"] == 1
    assert gen_files() == before                       # the other copy still uses them
    assert (await post(client, "scenes", [b])).json()["deleted"] == 1
    assert gen_files() == set()


async def test_only_admins_and_only_global_libraries(app, people, alice_lib):
    admin, alice, editor = people["admin"], people["alice"], people["editor"]
    for c in (alice, editor):
        assert (await post(c, "scenes", [alice_lib["scene"]])).status_code == 403
    # private: the admin can't even see it
    assert (await post(admin, "scenes", [alice_lib["scene"]])).json() == {
        "deleted": 0, "files": 0, "bytes": 0, "skipped": [{"id": alice_lib["scene"], "reason": "not found"}]}
    # shared with everyone: visible, but still the owner's to delete
    await alice.patch(f"/api/my/libraries/{alice_lib['id']}", json={"visibility": "everyone"})
    r = await post(admin, "scenes", [alice_lib["scene"]])
    assert r.json()["deleted"] == 0 and "user's own library" in r.json()["skipped"][0]["reason"]
    assert (await alice.get(f"/api/scenes/{alice_lib['scene']}")).status_code == 200


# --- duplicates -----------------------------------------------------------------------------

async def test_delete_duplicate_copies(app, client, indexed):
    pool = app.state.pool
    root = indexed["root"]
    (root / "other").mkdir()
    for name in ("b.mp4", "c.mp4"):
        shutil.copy(root / "videos" / "h264.mp4", root / "other" / name)
    await rescan(app)
    group = (await client.get("/api/duplicates")).json()["groups"][0]
    keeper = next(i for i in group["items"] if i["keep"])
    extras = [i for i in group["items"] if not i["keep"]]
    await client.patch(f"/api/scenes/{extras[0]['id']}", json={"rating": 60})

    # one copy: its details move to the others first, then the file goes
    r = await client.post("/api/duplicates/delete", json={"kind": "scene", "id": extras[0]["id"]})
    assert r.status_code == 200, r.text
    assert not os.path.exists(extras[0]["path"]) and os.path.exists(keeper["path"])
    assert (await client.get(f"/api/scenes/{keeper['id']}")).json()["rating"] == 60

    # A different file that merely has the same fingerprint is never deleted. Tried with the
    # look-alike both older and newer than the real copies: the job only ever deletes a
    # copy that is byte-identical to the one it keeps.
    fake = extras[1]
    vp9 = indexed["scenes"]["vp9"]
    await pool.execute("UPDATE files SET oshash = (SELECT oshash FROM files WHERE path = $1), size = (SELECT size FROM files WHERE path = $1) WHERE path = $2",
                       keeper["path"], str(root / "videos" / "vp9.webm"))
    r = await client.post("/api/duplicates/delete", json={"kind": "scene", "id": vp9})
    assert r.status_code == 409 and "not identical" in r.json()["detail"]
    assert (root / "videos" / "vp9.webm").exists()

    first = await pool.fetchval("SELECT f.path FROM files f WHERE f.oshash = (SELECT oshash FROM files WHERE path = $1) ORDER BY f.id LIMIT 1", keeper["path"])
    job_id = await pool.fetchval("INSERT INTO jobs (kind, status) VALUES ('delete_duplicates', 'running') RETURNING id")
    msg = await worker.HANDLERS["delete_duplicates"](pool, get_settings(), JobContext(pool, job_id), {"kind": "scene"})
    assert (root / "videos" / "vp9.webm").exists()
    if first.endswith("vp9.webm"):       # the look-alike is the one kept: nothing matches it
        assert msg.startswith("deleted 0 extra copies") and "2 kept because they are not identical" in msg
        assert os.path.exists(keeper["path"]) and os.path.exists(fake["path"])
        os.unlink(fake["path"])
        await rescan(app)
    else:                                # a real copy is kept: its twin goes, the look-alike stays
        assert msg.startswith("deleted 1 extra copies") and "1 kept because they are not identical" in msg
        assert os.path.exists(first) and sum(os.path.exists(p) for p in (keeper["path"], fake["path"])) == 1
    # the last real copy can't be removed through the duplicates tool
    await pool.execute("UPDATE files SET oshash = 'ffffffffffffffff' WHERE path LIKE '%vp9.webm'")
    keeper = (await client.get("/api/scenes")).json()["items"]
    keeper = next(s for s in keeper if s["id"] != vp9 and s["id"] != indexed["scenes"]["hevc"])
    r = await client.post("/api/duplicates/delete", json={"kind": "scene", "id": keeper["id"]})
    assert r.status_code == 409 and "only copy" in r.json()["detail"]

    assert (await client.post("/api/duplicates/delete-extras", json={"kind": "scene"})).status_code == 202
