import os
import shutil
from pathlib import Path

import pytest

from app import worker
from app.config import get_settings


async def test_library_validation(client, clean, library_dir):
    assert (await client.post("/api/libraries", json={"path": "relative"})).status_code == 422
    assert (await client.post("/api/libraries", json={"path": "/etc"})).status_code == 422
    missing = os.path.join(os.environ["MEDIA_DIR"], "nope")
    assert (await client.post("/api/libraries", json={"path": missing})).status_code == 422

    r = await client.post("/api/libraries", json={"path": str(library_dir), "watch": True})
    assert r.status_code == 201, r.text
    assert (await client.post("/api/libraries", json={"path": str(library_dir / "videos")})).status_code == 409
    assert (await client.post("/api/libraries", json={"path": os.environ["MEDIA_DIR"]})).status_code == 409


async def test_generated_dir_cannot_be_a_library(client, clean, monkeypatch):
    gen = Path(os.environ["MEDIA_DIR"]) / "gen"
    gen.mkdir(exist_ok=True)
    monkeypatch.setattr(get_settings(), "generated_dir", gen)
    r = await client.post("/api/libraries", json={"path": str(gen)})
    assert r.status_code == 422 and "generated" in r.text


async def test_scan_via_api_and_worker(app, client, clean, library_dir):
    lib = (await client.post("/api/libraries", json={"path": str(library_dir)})).json()
    first = (await client.post(f"/api/libraries/{lib['id']}/scan")).json()["job_id"]
    again = (await client.post(f"/api/libraries/{lib['id']}/scan")).json()["job_id"]
    assert first == again

    settings = get_settings()
    settings_copy = settings.model_copy(update={"scan_settle_seconds": 0})
    assert await worker.run_one(app.state.pool, settings_copy)
    job = (await client.get(f"/api/jobs/{first}")).json()
    assert job["status"] == "done", job
    assert "7 new" in job["message"]

    listing = (await client.get("/api/jobs", params={"limit": 2})).json()
    assert {j["kind"] for j in listing["items"]} == {"generate", "refresh_counts"}  # queued by the scan
    assert listing["next"] is not None
    assert (await client.get("/api/jobs", params={"status": "bogus"})).status_code == 422


async def test_scan_of_unavailable_library_fails_job(app, client, clean, library_dir):
    lib = (await client.post("/api/libraries", json={"path": str(library_dir)})).json()
    job_id = (await client.post(f"/api/libraries/{lib['id']}/scan")).json()["job_id"]
    library_dir.rename(library_dir.with_name(library_dir.name + "-gone"))
    try:
        await worker.run_one(app.state.pool, get_settings())
        job = (await client.get(f"/api/jobs/{job_id}")).json()
        assert job["status"] == "failed" and "not available" in job["error"]
    finally:
        library_dir.with_name(library_dir.name + "-gone").rename(library_dir)


async def test_delete_library_removes_index_not_files(app, client, clean, library_dir):
    lib = (await client.post("/api/libraries", json={"path": str(library_dir)})).json()
    await client.post(f"/api/libraries/{lib['id']}/scan")
    await worker.run_one(app.state.pool, get_settings().model_copy(update={"scan_settle_seconds": 0}))
    assert (await client.delete(f"/api/libraries/{lib['id']}")).status_code == 204
    pool = app.state.pool
    for table in ("files", "scenes", "images", "galleries"):
        assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0, table
    assert (library_dir / "videos" / "h264.mp4").exists()


# --- folder browser & selective scans ------------------------------------------

@pytest.fixture
def tree(library_dir):
    """library_dir + extra structure: artists/{Artist 2, Artist 10}/..., hidden + symlinked dirs."""
    (library_dir / "artists" / "Artist 10").mkdir(parents=True)
    (library_dir / "artists" / "Artist 2" / "set").mkdir(parents=True)
    (library_dir / ".hidden").mkdir()
    (library_dir / "linked").symlink_to(library_dir / "videos")
    (library_dir / "file.txt").write_text("x")
    return library_dir


async def test_browse_lists_visible_subdirectories(client, clean, tree):
    root = os.environ["MEDIA_DIR"]
    top = (await client.get("/api/libraries/browse")).json()
    assert top["path"] == top["root"] == root and top["parent"] is None
    assert str(tree) in [e["path"] for e in top["entries"]]

    body = (await client.get("/api/libraries/browse", params={"path": str(tree)})).json()
    assert [e["name"] for e in body["entries"]] == ["artists", "pics", "videos"]  # no hidden/symlink/file
    assert body["parent"] == root and body["library"] is None

    artists = (await client.get("/api/libraries/browse", params={"path": str(tree / "artists")})).json()
    assert [e["name"] for e in artists["entries"]] == ["Artist 2", "Artist 10"]  # natural order


async def test_browse_marks_libraries(client, clean, tree):
    lib = (await client.post("/api/libraries", json={"path": str(tree)})).json()
    top = (await client.get("/api/libraries/browse")).json()
    assert next(e for e in top["entries"] if e["path"] == str(tree))["library_id"] == lib["id"]
    inside = (await client.get("/api/libraries/browse", params={"path": str(tree / "artists")})).json()
    assert inside["library"] == {"id": lib["id"], "path": str(tree)}


async def test_browse_stays_inside_media_root(client, clean, tree):
    assert (await client.get("/api/libraries/browse", params={"path": "/etc"})).status_code == 422
    escape = f"{tree}/../../../etc"
    assert (await client.get("/api/libraries/browse", params={"path": escape})).status_code == 422
    assert (await client.get("/api/libraries/browse", params={"path": "relative"})).status_code == 422
    assert (await client.get("/api/libraries/browse", params={"path": str(tree / "nope")})).status_code == 404
    assert (await client.get("/api/libraries/browse", params={"path": str(tree / "linked")})).status_code == 200  # inside root


async def test_browse_is_admin_only(login_as, clean, tree):
    editor = await login_as("ed", "editor")
    assert (await editor.get("/api/libraries/browse")).status_code == 403


async def test_scan_single_subfolder(app, client, clean, tree, sample_media):
    lib = (await client.post("/api/libraries", json={"path": str(tree)})).json()
    settings = get_settings().model_copy(update={"scan_settle_seconds": 0})
    await client.post(f"/api/libraries/{lib['id']}/scan")
    await worker.run_one(app.state.pool, settings)
    pool = app.state.pool
    before = await pool.fetchval("SELECT count(*) FROM files WHERE zip_file_id IS NULL")

    # change things both inside and outside the chosen folder
    shutil.copy(sample_media / "h264.mp4", tree / "artists" / "Artist 2" / "new.mp4")
    (tree / "videos" / "vp9.webm").unlink()  # outside: must NOT be noticed by this scan

    r = await client.post(f"/api/libraries/{lib['id']}/scan", json={"path": str(tree / "artists" / "Artist 2")})
    job_id = r.json()["job_id"]
    assert await pool.fetchval("SELECT payload->>'path' FROM jobs WHERE id = $1", job_id) == str(tree / "artists" / "Artist 2")
    await pool.execute("UPDATE jobs SET status = 'cancelled' WHERE id <> $1 AND status = 'queued'", job_id)
    await worker.run_one(app.state.pool, settings)
    job = (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["status"] == "done" and job["message"] == "1 new"
    assert await pool.fetchval("SELECT count(*) FROM files WHERE zip_file_id IS NULL") == before + 1
    assert await pool.fetchval("SELECT count(*) FROM files WHERE missing_since IS NOT NULL") == 0


async def test_scan_subfolder_validation(client, clean, tree):
    lib = (await client.post("/api/libraries", json={"path": str(tree)})).json()
    url = f"/api/libraries/{lib['id']}/scan"
    assert (await client.post(url, json={"path": "/etc"})).status_code == 422
    r = await client.post(url, json={"path": str(tree / "typo")})
    assert r.status_code == 422 and "does not exist" in r.text
    # scanning the library root itself is just a full scan
    full = (await client.post(url, json={"path": str(tree)})).json()["job_id"]
    assert full == (await client.post(url)).json()["job_id"]
