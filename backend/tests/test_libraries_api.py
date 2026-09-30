import os
from pathlib import Path

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

    listing = (await client.get("/api/jobs", params={"limit": 1})).json()
    assert listing["items"][0]["kind"] == "generate"  # the scan queued asset generation
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
