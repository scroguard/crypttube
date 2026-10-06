"""Repairing and force-rebuilding generated content."""

import io
import os

from PIL import Image

from app import generated, worker
from app.config import get_settings
from app.jobs import JobContext
from tests.test_media_api import indexed  # noqa: F401  (fixture)


async def run_generate(app, payload):
    pool = app.state.pool
    job_id = await pool.fetchval(
        "INSERT INTO jobs (kind, payload, status) VALUES ('generate', $1, 'running') RETURNING id", payload)
    return await worker.handle_generate(pool, get_settings(), JobContext(pool, job_id), payload)


async def scene_files(app, scene_id):
    r = await app.state.pool.fetchrow(
        "SELECT f.oshash, f.id FROM scenes s JOIN files f ON f.id = s.file_id WHERE s.id = $1", scene_id)
    version = generated.scene_version(r["oshash"], r["id"])
    root = get_settings().generated_dir
    return generated.screenshot_path(root, version), *generated.sprite_paths(root, version)


def is_image(data: bytes) -> bool:
    try:
        Image.open(io.BytesIO(data)).verify()
        return True
    except Exception:
        return False


async def test_generate_missing_also_repairs_empty_files(app, client, indexed):
    sid = indexed["scenes"]["h264"]
    await run_generate(app, {"missing": True})
    shot, sprite, vtt = await scene_files(app, sid)
    shot.write_bytes(b"")          # what an interrupted/failed write can leave behind
    vtt.write_bytes(b"")
    # on-demand serving treats the empty file as missing and regenerates it
    r = await client.get(f"/api/scenes/{sid}/screenshot")
    assert r.status_code == 200 and is_image(r.content)
    shot.write_bytes(b"")
    assert await run_generate(app, {"missing": True}) == "1 generated"
    assert shot.stat().st_size > 0 and vtt.read_text().startswith("WEBVTT")
    assert await run_generate(app, {"missing": True}) == "0 generated"


async def test_force_rebuild_replaces_bad_files_and_busts_caches(app, client, indexed):
    sid = indexed["scenes"]["h264"]
    await run_generate(app, {"missing": True})
    old_url = (await client.get(f"/api/scenes/{sid}/playback")).json()["screenshot_url"]
    assert "immutable" in (await client.get(old_url)).headers["cache-control"]

    shot, sprite, vtt = await scene_files(app, sid)
    shot.write_bytes(b"this is not an image, but it is not empty either")
    assert not is_image((await client.get(old_url)).content)       # "generate missing" can't know
    assert await run_generate(app, {"missing": True}) == "0 generated"

    assert (await run_generate(app, {"force": True})).split(";")[0] == "3 rebuilt"
    info = (await client.get(f"/api/scenes/{sid}/playback")).json()
    new_url = info["screenshot_url"]
    assert new_url != old_url and new_url.endswith(".1")            # revision in the version
    fresh = await client.get(new_url)
    assert is_image(fresh.content) and "immutable" in fresh.headers["cache-control"]
    # a browser still holding the old URL revalidates instead of trusting its cached copy
    assert (await client.get(old_url)).headers["cache-control"] == "private, no-cache"
    listed = (await client.get("/api/scenes", params={"q": "h264"})).json()["items"][0]["screenshot_url"]
    assert listed.split("v=")[1] == new_url.split("v=")[1]
    # the sprite sheet reference inside the VTT carries the new version too
    vtt_text = (await client.get(info["sprite_vtt_url"])).text
    assert f"sprite.webp?v={new_url.split('v=')[1]}#xywh=" in vtt_text
    assert await app.state.pool.fetchval("SELECT assets_rev FROM scenes WHERE id = $1", sid) == 1


async def test_force_rebuild_resets_image_thumbnails(app, client, indexed):
    img = next(i for p, i in indexed["images"].items() if p.endswith("pics/img10.jpg"))
    item = (await client.get(f"/api/images/{img}")).json()
    old = item["thumbnail_url"]
    assert is_image((await client.get(old)).content)
    r = await app.state.pool.fetchrow("SELECT f.id, f.mod_time FROM images i JOIN files f ON f.id = i.file_id WHERE i.id = $1", img)
    cached = generated.thumbnail_path(get_settings().generated_dir, "images",
                                      generated.image_version(r["id"], r["mod_time"]), 1280)
    cached.write_bytes(b"corrupt thumbnail")
    assert not is_image((await client.get(old)).content)

    await run_generate(app, {"force": True})
    new = (await client.get(f"/api/images/{img}")).json()["thumbnail_url"]
    assert new != old and is_image((await client.get(new)).content)
    # gallery covers follow
    cover = (await client.get("/api/galleries")).json()["items"][0]["cover_url"]
    assert cover.split("v=")[1].endswith(".1")


async def test_force_rebuild_can_be_limited_to_a_library_or_scene(app, client, indexed, library_dir, sample_media):
    import shutil
    import tempfile
    from app.scanner import Scanner
    pool = app.state.pool
    other = tempfile.mkdtemp(dir=os.environ["MEDIA_DIR"])
    shutil.copy(sample_media / "vp9.webm", os.path.join(other, "solo.webm"))
    shutil.copy(sample_media / "img0.jpg", os.path.join(other, "solo.jpg"))
    lib2 = await pool.fetchval("INSERT INTO library_paths (path) VALUES ($1) RETURNING id", other)
    await Scanner(pool, get_settings().generated_dir, settle_seconds=0).scan(lib2)
    await run_generate(app, {"missing": True})

    assert (await run_generate(app, {"force": True, "library_id": lib2})).split(";")[0] == "1 rebuilt"
    revs = dict(await pool.fetch("SELECT library_id, max(assets_rev) FROM scenes GROUP BY 1"))
    lib1 = next(k for k in revs if k != lib2)
    assert revs == {lib1: 0, lib2: 1}
    assert dict(await pool.fetch("SELECT library_id, max(assets_rev) FROM images GROUP BY 1")) == {lib1: 0, lib2: 1}

    sid = indexed["scenes"]["h264"]
    assert await run_generate(app, {"force": True, "scene_ids": [sid]}) == "1 rebuilt"
    assert await pool.fetchval("SELECT assets_rev FROM scenes WHERE id = $1", sid) == 1
    assert await pool.fetchval("SELECT max(assets_rev) FROM images WHERE library_id = $1", lib1) == 0
    shutil.rmtree(other)


async def test_failures_are_reported_with_reasons(app, client, indexed):
    root = indexed["root"]
    (root / "videos" / "h265.mkv").write_bytes(b"corrupted on disk after indexing" * 10)
    msg = await run_generate(app, {"force": True})
    first, *details = msg.splitlines()
    assert first.split(";")[0] == "2 rebuilt, 1 failed"
    assert len(details) == 1 and details[0].startswith(f"scene {indexed['scenes']['hevc']} (h265.mkv): ")


async def test_uploaded_entity_images_survive_a_full_rebuild(app, client, indexed):
    tag = (await client.post("/api/tags", json={"name": "Keep"})).json()["id"]
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(buf, "PNG")
    assert (await client.put(f"/api/tags/{tag}/image", content=buf.getvalue())).status_code == 204
    await run_generate(app, {"force": True})
    url = (await client.get(f"/api/tags/{tag}")).json()["image_url"]
    assert (await client.get(url)).status_code == 200


async def test_unwritable_volume_fails_the_job_with_an_explanation(app, client, indexed):
    root = get_settings().generated_dir
    os.chmod(root, 0o555)
    try:
        job = (await client.post("/api/generate", json={"force": True})).json()["job_id"]
        await app.state.pool.execute("UPDATE jobs SET status = 'cancelled' WHERE status = 'queued' AND id <> $1", job)
        await worker.run_one(app.state.pool, get_settings())
        result = (await client.get(f"/api/jobs/{job}")).json()
        assert result["status"] == "failed"
        assert "not writable" in result["error"] and f"uid {os.getuid()}" in result["error"]
    finally:
        os.chmod(root, 0o777)


async def test_rebuild_endpoints_and_permissions(app, client, login_as, indexed):
    sid = indexed["scenes"]["h264"]
    pool = app.state.pool
    lib = await pool.fetchval("SELECT id FROM library_paths")
    viewer = await login_as("vera", "viewer")
    editor = await login_as("ed2", "editor")

    assert (await viewer.post("/api/generate", json={"force": True})).status_code == 403
    assert (await viewer.post(f"/api/scenes/{sid}/regenerate")).status_code == 403   # global scene, not an editor
    assert (await editor.post(f"/api/scenes/{sid}/regenerate")).status_code == 202
    assert (await client.post("/api/scenes/999999/regenerate")).status_code == 404
    assert (await client.post("/api/generate", json={"library_id": 999999})).status_code == 404

    r = await client.post("/api/generate", json={"force": True, "library_id": lib})
    again = await client.post("/api/generate", json={"force": True, "library_id": lib})
    assert r.status_code == 202 and r.json() == again.json()                          # collapsed while queued
    payloads = [p["payload"] for p in await pool.fetch("SELECT payload FROM jobs WHERE kind = 'generate' ORDER BY id")]
    assert payloads == [{"scene_ids": [sid], "force": True}, {"force": True, "library_id": lib}]
    assert (await client.post("/api/generate")).status_code == 202                    # plain "generate missing" still works


async def test_thumbnails_are_ready_before_anyone_browses(app, client, clean, library_dir):
    """A scan queues generation that also makes the grid thumbnails of every new image
    and scene, so the first page view is served from files."""
    from tests.test_user_libraries import drain
    pool = app.state.pool
    lid = await pool.fetchval("INSERT INTO library_paths (path) VALUES ($1) RETURNING id", str(library_dir))
    await pool.execute("INSERT INTO jobs (kind, payload) VALUES ('scan', $1)", {"library_id": lid, "settle": 0})
    await drain(app)
    root = get_settings().generated_dir
    images = await pool.fetch(
        """SELECT i.id, f.id AS file_id, f.mod_time, EXISTS (SELECT 1 FROM galleries g WHERE g.cover_image_id = i.id) AS cover
           FROM images i JOIN files f ON f.id = i.file_id""")
    assert len(images) == 5          # 3 in pics/, 2 in archive.zip
    for r in images:
        version = generated.image_version(r["file_id"], r["mod_time"])
        assert generated.usable(generated.thumbnail_path(root, "images", version, generated.IMAGE_GRID_WIDTH))
        assert generated.usable(generated.thumbnail_path(root, "images", version, generated.COVER_WIDTH)) == r["cover"]
    assert sum(r["cover"] for r in images) == 2
    for r in await pool.fetch("SELECT f.oshash, f.id FROM scenes s JOIN files f ON f.id = s.file_id"):
        version = generated.scene_version(r["oshash"], r["id"])
        assert generated.usable(generated.thumbnail_path(root, "scenes", version, generated.SCENE_GRID_WIDTH))
    # nothing more to do the second time
    assert await run_generate(app, {"missing": True}) == "0 generated"
