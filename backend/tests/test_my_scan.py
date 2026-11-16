"""Users scanning their own libraries (POST /api/my/libraries/{id}/scan) and the cooldown
on users' scan and rebuild requests."""

from pathlib import Path

import pytest

from app.config import get_settings
from tests.test_user_libraries import drain, people, upload  # noqa: F401  (fixtures)


async def make_lib(c, name="Mine") -> tuple[int, None]:
    r = await c.post("/api/my/libraries", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"], None


async def lib_path(app, lib_id) -> Path:
    return Path(await app.state.pool.fetchval("SELECT path FROM library_paths WHERE id = $1", lib_id))


@pytest.fixture
def no_cooldown(monkeypatch):
    monkeypatch.setattr(get_settings(), "my_library_cooldown_seconds", 0)


async def test_owner_scans_and_files_copied_in_are_indexed(app, people, sample_media, no_cooldown):
    alice = people["alice"]
    lib, _ = await make_lib(alice)
    root = await lib_path(app, lib)
    (root / "copied").mkdir()
    (root / "copied" / "clip.mp4").write_bytes((sample_media / "h264.mp4").read_bytes())
    assert (await alice.get("/api/scenes")).json()["items"] == []
    r = await alice.post(f"/api/my/libraries/{lib}/scan")
    assert r.status_code == 202
    job = await app.state.pool.fetchrow("SELECT kind, payload FROM jobs WHERE id = $1", r.json()["job_id"])
    assert job["kind"] == "scan" and job["payload"] == {"library_id": lib, "path": None, "requested": "user"}
    await drain(app)
    assert len((await alice.get("/api/scenes")).json()["items"]) == 1


async def test_one_folder(app, people, no_cooldown):
    alice = people["alice"]
    lib, _ = await make_lib(alice)
    root = await lib_path(app, lib)
    (root / "album").mkdir()
    r = await alice.post(f"/api/my/libraries/{lib}/scan", json={"path": "album/"})
    assert r.status_code == 202
    payload = await app.state.pool.fetchval("SELECT payload FROM jobs WHERE id = $1", r.json()["job_id"])
    assert payload["path"] == str(root / "album")
    for bad, code in (("../", 422), ("album/../..", 422), (".hidden", 422), ("nope", 422)):
        assert (await alice.post(f"/api/my/libraries/{lib}/scan", json={"path": bad})).status_code == code, bad
    assert (await alice.post(f"/api/my/libraries/{lib}/scan", json={"other": 1})).status_code == 422


async def test_only_the_owner_can_scan(app, people, no_cooldown):
    alice, bob, admin = people["alice"], people["bob"], people["admin"]
    lib, _ = await make_lib(alice)
    await alice.patch(f"/api/my/libraries/{lib}", json={"visibility": "everyone"})
    assert (await bob.post(f"/api/my/libraries/{lib}/scan")).status_code == 404       # even when shared
    assert (await admin.post(f"/api/my/libraries/{lib}/scan")).status_code == 404     # admins use their own route
    assert (await alice.post("/api/my/libraries/999999/scan")).status_code == 404
    assert (await bob.post(f"/api/my/libraries/{lib}/regenerate")).status_code == 404
    assert await app.state.pool.fetchval("SELECT count(*) FROM jobs WHERE payload->>'requested' = 'user'") == 0


async def test_cooldown_on_scan_and_rebuild(app, people, sample_media):
    alice = people["alice"]
    lib, _ = await make_lib(alice)
    # an upload's own scan doesn't count against the user's request. (While it is still queued,
    # a request merges into it: no extra work, so no cooldown either.)
    assert (await upload(alice, lib, "clip.mp4", (sample_media / "h264.mp4").read_bytes())).status_code == 201
    first = (await alice.post(f"/api/my/libraries/{lib}/scan")).json()["job_id"]
    assert await app.state.pool.fetchval("SELECT payload->>'requested' FROM jobs WHERE id = $1", first) is None
    await drain(app)
    assert (await alice.post(f"/api/my/libraries/{lib}/scan")).status_code == 202
    r = await alice.post(f"/api/my/libraries/{lib}/scan")
    assert r.status_code == 429
    wait = int(r.headers["Retry-After"])
    assert 0 < wait <= get_settings().my_library_cooldown_seconds + 1
    # rebuild has its own cooldown
    assert (await alice.post(f"/api/my/libraries/{lib}/regenerate")).status_code == 202
    r = await alice.post(f"/api/my/libraries/{lib}/regenerate")
    assert r.status_code == 429 and "Retry-After" in r.headers
    # per library: another library isn't held back
    other, _ = await make_lib(alice, "Other")
    assert (await alice.post(f"/api/my/libraries/{other}/scan")).status_code == 202


async def test_repeated_requests_make_one_job(app, people, no_cooldown):
    alice = people["alice"]
    lib, _ = await make_lib(alice)
    ids = {(await alice.post(f"/api/my/libraries/{lib}/scan")).json()["job_id"] for _ in range(3)}
    assert len(ids) == 1
    assert await app.state.pool.fetchval(
        "SELECT count(*) FROM jobs WHERE kind = 'scan' AND status = 'queued' AND payload->>'library_id' = $1", str(lib)) == 1
