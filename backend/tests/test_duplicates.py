"""Finding identical files under different names, and combining their details."""

import shutil
from pathlib import Path

from app.config import get_settings
from app.scanner import Scanner
from tests.test_media_api import indexed  # noqa: F401  (fixture)
from tests.test_user_libraries import alice_lib, people  # noqa: F401  (fixtures)


async def rescan(app):
    pool = app.state.pool
    for r in await pool.fetch("SELECT id FROM library_paths"):
        await Scanner(pool, get_settings().generated_dir, settle_seconds=0).scan(r["id"])


async def test_find_combine_and_export(app, client, indexed):
    pool = app.state.pool
    root = indexed["root"]
    body = (await client.get("/api/duplicates")).json()
    assert body["groups"] == [] and body["summary"]["scene"] == {"groups": 0, "extra_copies": 0, "extra_bytes": 0}
    # the fixture already has one duplicated picture: pics/img1.jpg is the file inside the zip too,
    # but zip members are not compared, so no image duplicates either
    assert body["summary"]["image"]["groups"] == 0

    (root / "other").mkdir()
    shutil.copy(root / "videos" / "h264.mp4", root / "other" / "renamed copy.mp4")
    shutil.copy(root / "videos" / "h264.mp4", root / "other" / "third.mp4")
    shutil.copy(root / "pics" / "img2.jpg", root / "other" / "same picture.jpg")
    await rescan(app)

    size = (root / "videos" / "h264.mp4").stat().st_size
    body = (await client.get("/api/duplicates", params={"kind": "scene"})).json()
    assert body["summary"]["scene"] == {"groups": 1, "extra_copies": 2, "extra_bytes": 2 * size}
    assert body["summary"]["image"]["groups"] == 1 and body["has_more"] is False
    (group,) = body["groups"]
    assert group["size"] == size and group["in_sync"] is True
    assert sorted(i["path"].rsplit("/", 1)[1] for i in group["items"]) == ["h264.mp4", "renamed copy.mp4", "third.mp4"]
    assert [i["keep"] for i in group["items"]] == [True, False, False]
    assert (await client.get(group["items"][1]["thumbnail_url"])).status_code == 200

    # details live on different copies
    a, b, c = (i["id"] for i in group["items"])
    t1 = (await client.post("/api/tags", json={"name": "One"})).json()["id"]
    t2 = (await client.post("/api/tags", json={"name": "Two"})).json()["id"]
    perf = (await client.post("/api/performers", json={"name": "Pat"})).json()["id"]
    studio = (await client.post("/api/studios", json={"name": "Stu"})).json()["id"]
    await client.patch(f"/api/scenes/{a}", json={"tag_ids": [t1], "rating": 80})
    await client.patch(f"/api/scenes/{b}", json={"tag_ids": [t2], "performer_ids": [perf], "rating": 40, "studio_id": studio})
    await client.patch(f"/api/scenes/{c}", json={"details": "notes", "organized": True})
    group = (await client.get("/api/duplicates")).json()["groups"][0]
    assert group["in_sync"] is False and group["items"][1]["performers"] == ["Pat"]

    r = await client.post("/api/duplicates/combine", json={"kind": "scene", "id": b})
    assert r.status_code == 200 and r.json() == {"groups": 1}
    for sid, rating in ((a, 80), (b, 40), (c, 80)):       # an existing rating is kept; empty ones are filled
        s = (await client.get(f"/api/scenes/{sid}")).json()
        assert sorted(t["name"] for t in s["tags"]) == ["One", "Two"]
        assert [p["name"] for p in s["performers"]] == ["Pat"]
        assert (s["rating"], s["studio"]["name"], s["details"], s["organized"]) == (rating, "Stu", "notes", True)
    # scenes that are not copies are untouched
    other = (await client.get(f"/api/scenes/{indexed['scenes']['vp9']}")).json()
    assert other["tags"] == [] and other["rating"] is None

    # everything at once (images here)
    img = next(i for g in (await client.get("/api/duplicates", params={"kind": "image"})).json()["groups"] for i in g["items"])
    await client.patch(f"/api/images/{img['id']}", json={"tag_ids": [t1]})
    assert (await client.post("/api/duplicates/combine", json={"kind": "image"})).json() == {"groups": 1}
    g = (await client.get("/api/duplicates", params={"kind": "image"})).json()["groups"][0]
    assert g["in_sync"] and all(i["tags"] == ["One"] for i in g["items"])

    # the list of extra copies never includes the suggested keeper
    r = await client.get("/api/duplicates/extra-copies.txt", params={"kind": "scene"})
    assert r.headers["content-type"].startswith("text/plain") and "attachment" in r.headers["content-disposition"]
    extra = r.text.splitlines()
    assert extra == sorted(i["path"] for i in group["items"] if not i["keep"]) and len(extra) == 2

    # once the extra copies are deleted and rescanned, the group is gone
    for path in extra:
        Path(path).unlink()
    await rescan(app)
    assert (await client.get("/api/duplicates")).json()["summary"]["scene"]["groups"] == 0
    assert (await client.post("/api/duplicates/combine", json={"kind": "scene", "id": 999999})).status_code == 404
    assert await pool.fetchval("SELECT count(*) FROM scene_tags WHERE scene_id = $1", a) == 2


async def test_private_libraries_are_not_compared(app, client, indexed, people, alice_lib, sample_media):
    """A copy inside a user's private library must not show up for the admin."""
    # alice's library holds a copy of h264.mp4 (uploaded by the fixture)
    assert await app.state.pool.fetchval(
        "SELECT count(*) FROM files WHERE oshash = (SELECT oshash FROM files WHERE path LIKE '%videos/h264.mp4')") == 2
    assert (await client.get("/api/duplicates")).json()["summary"]["scene"]["groups"] == 0
    assert (await client.get("/api/duplicates/extra-copies.txt")).text == ""


async def test_admins_only(client, clean, login_as, anon):
    for c in (await login_as("ed", "editor"), await login_as("vera", "viewer")):
        assert (await c.get("/api/duplicates")).status_code == 403
        assert (await c.post("/api/duplicates/combine", json={"kind": "scene"})).status_code == 403
        assert (await c.get("/api/duplicates/extra-copies.txt")).status_code == 403
    assert (await anon.get("/api/duplicates")).status_code == 401
