import re

import pytest

from app import auth
from tests.conftest import TEST_ADMIN
from tests.test_browse import seed  # noqa: F401  (fixture)

PUBLIC = {"/api/health", "/api/auth/status", "/api/auth/setup", "/api/auth/login"}


async def test_every_route_requires_a_session(app, anon):
    checked = 0
    for path, ops in app.openapi()["paths"].items():
        if path in PUBLIC:
            continue
        concrete = re.sub(r"\{[^}]+\}", "1", path)
        for method in ops:
            r = await anon.request(method.upper(), concrete)
            assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code}"
            checked += 1
    assert checked > 50


async def test_role_requirements(app, client, login_as, seed):
    viewer = await login_as("vic", "viewer")
    editor = await login_as("eddie", "editor")
    sid = seed["scenes"][1]
    for c in (viewer, editor, client):
        assert (await c.get("/api/scenes")).status_code == 200
        assert (await c.get(f"/api/scenes/{sid}/playback")).status_code in (200, 404)  # seed files aren't on disk
        assert (await c.put(f"/api/scenes/{sid}/resume", json={"time": 1})).status_code == 204
    assert (await viewer.patch(f"/api/scenes/{sid}", json={"title": "x"})).status_code == 403
    assert (await editor.patch(f"/api/scenes/{sid}", json={"title": "x"})).status_code == 200
    for c in (viewer, editor):
        for path in ("/api/users", "/api/libraries", "/api/jobs"):
            assert (await c.get(path)).status_code == 403, path
        assert (await c.post("/api/generate")).status_code == 403
    assert (await client.get("/api/users")).status_code == 200


async def test_csrf_header_required_for_writes(client, seed):
    sid = seed["scenes"][1]
    r = await client.patch(f"/api/scenes/{sid}", json={"title": "x"}, headers={"X-Requested-With": ""})
    assert r.status_code == 403 and "x-requested-with" in r.text
    del client.headers["X-Requested-With"]
    assert (await client.get("/api/scenes")).status_code == 200  # reads don't need it
    assert (await client.post("/api/auth/login", json={"username": "a", "password": "b"})).status_code == 403


# --- setup & login ---------------------------------------------------------------

async def test_first_run_setup(app, anon):
    pool = app.state.pool
    await pool.execute("DELETE FROM users")
    assert (await anon.get("/api/auth/status")).json() == {"setup_required": True, "user": None}
    assert (await anon.post("/api/auth/setup", json={"username": "root", "password": "short"})).status_code == 422
    r = await anon.post("/api/auth/setup", json={"username": "root", "password": "correct horse"})
    assert r.status_code == 201 and r.json()["role"] == "admin"
    assert anon.cookies.get(auth.COOKIE_NAME)  # signed in straight away
    status = (await anon.get("/api/auth/status")).json()
    assert status == {"setup_required": False, "user": {"id": r.json()["id"], "username": "root", "role": "admin", "can_upload": False}}
    again = await anon.post("/api/auth/setup", json={"username": "evil", "password": "correct horse"})
    assert again.status_code == 409


async def test_login_logout_cycle(app, anon, login_as):
    await login_as("lou", "viewer")  # creates lou / password123
    r = await anon.post("/api/auth/login", json={"username": "LOU", "password": "password123"})
    assert r.status_code == 200 and r.json()["username"] == "lou"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert (await anon.get("/api/auth/me")).json()["can_edit"] is False
    token = anon.cookies.get(auth.COOKIE_NAME)
    stored = await app.state.pool.fetchval("SELECT count(*) FROM sessions WHERE token_hash = $1", token.encode())
    assert stored == 0  # only the hash is stored

    assert (await anon.post("/api/auth/logout")).status_code == 204
    anon.cookies.set(auth.COOKIE_NAME, token)  # replaying the old token
    assert (await anon.get("/api/auth/me")).status_code == 401


async def test_bad_credentials_and_throttle(app, anon, login_as, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(auth, "throttle", auth.LoginThrottle(clock=lambda: clock[0]))
    await login_as("tess", "viewer")
    bad = {"username": "tess", "password": "wrong-password"}
    for _ in range(3):
        assert (await anon.post("/api/auth/login", json=bad)).status_code == 401
    assert (await anon.post("/api/auth/login", json=bad)).status_code == 401  # 4th failure starts backoff
    r = await anon.post("/api/auth/login", json={"username": "tess", "password": "password123"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 2  # even correct password waits
    clock[0] += 3
    assert (await anon.post("/api/auth/login", json={"username": "tess", "password": "password123"})).status_code == 200
    # unknown users get the same answer as wrong passwords
    r = await anon.post("/api/auth/login", json={"username": "nobody", "password": "whatever1"})
    assert r.status_code == 401 and r.json()["detail"] == "invalid username or password"


async def test_disabled_user_cannot_log_in(app, anon, client, login_as):
    u = await login_as("gone", "viewer")
    await client.patch(f"/api/users/{u.user_id}", json={"is_active": False})
    assert (await u.get("/api/auth/me")).status_code == 401  # existing session killed
    r = await anon.post("/api/auth/login", json={"username": "gone", "password": "password123"})
    assert r.status_code == 401


async def test_password_change_and_sessions(app, anon, login_as):
    u = await login_as("pat", "viewer")
    other = await anon.post("/api/auth/login", json={"username": "pat", "password": "password123"})
    assert other.status_code == 200
    sessions = (await u.get("/api/auth/sessions")).json()
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1

    r = await u.post("/api/auth/password", json={"current_password": "nope", "new_password": "new-password-1"})
    assert r.status_code == 403
    r = await u.post("/api/auth/password", json={"current_password": "password123", "new_password": "new-password-1"})
    assert r.status_code == 204
    assert (await u.get("/api/auth/me")).status_code == 200      # this session survives
    assert (await anon.get("/api/auth/me")).status_code == 401   # the other was signed out
    r = await anon.post("/api/auth/login", json={"username": "pat", "password": "new-password-1"})
    assert r.status_code == 200


# --- user administration ----------------------------------------------------------

async def test_user_admin(app, client):
    r = await client.post("/api/users", json={"username": "newbie", "password": "password123", "role": "editor"})
    assert r.status_code == 201 and r.json()["role"] == "editor"
    uid = r.json()["id"]
    assert (await client.post("/api/users", json={"username": "NEWBIE", "password": "password123"})).status_code == 409
    assert (await client.post("/api/users", json={"username": "bad name", "password": "password123"})).status_code == 422
    assert (await client.patch(f"/api/users/{uid}", json={"role": "viewer"})).json()["role"] == "viewer"
    assert any(u["username"] == "newbie" for u in (await client.get("/api/users")).json())
    assert (await client.delete(f"/api/users/{uid}")).status_code == 204
    assert (await client.get(f"/api/users/{uid}")).status_code == 404


async def test_last_admin_is_protected(app, client):
    me = (await client.get("/api/auth/me")).json()["id"]
    await app.state.pool.execute("DELETE FROM users WHERE role = 'admin' AND username <> $1", TEST_ADMIN)
    assert (await client.patch(f"/api/users/{me}", json={"role": "viewer"})).status_code == 409
    assert (await client.patch(f"/api/users/{me}", json={"is_active": False})).status_code == 409
    assert (await client.delete(f"/api/users/{me}")).status_code == 409
    assert (await client.get("/api/auth/me")).json()["role"] == "admin"


async def test_restrictions_apply_through_real_sessions(app, client, login_as, seed, library_dir):
    """End to end: admin sets rules; the restricted user's lists, details, counts and
    media all hide the content. (Other ACL tests override the dependency; this one doesn't.)"""
    from app import counts
    viewer = await login_as("kid", "viewer")
    t = seed["tags"]
    r = await client.put(f"/api/users/{viewer.user_id}/restrictions",
                         json={"tag_ids": [t["Position"]], "performer_ids": [seed["performers"]["Bob"]]})
    assert r.status_code == 200
    assert [x["name"] for x in r.json()["restrictions"]["tags"]] == ["Position"]
    assert await app.state.pool.fetchval("SELECT count(*) FROM jobs WHERE kind = 'refresh_counts'") >= 1
    await counts.refresh(app.state.pool)

    visible = {s["id"] for s in (await viewer.get("/api/scenes", params={"limit": 100})).json()["items"]}
    everything = {s["id"] for s in (await client.get("/api/scenes", params={"limit": 100})).json()["items"]}
    hidden = seed["scenes"][0]  # even -> Standing (under Position)
    assert hidden in everything and hidden not in visible and visible < everything
    assert (await viewer.get(f"/api/scenes/{hidden}")).status_code == 404
    assert (await viewer.get(f"/api/scenes/{hidden}/stream")).status_code == 404
    assert (await viewer.get(f"/api/scenes/{hidden}/screenshot")).status_code == 404
    tags = {x["name"] for x in (await viewer.get("/api/tags")).json()["items"]}
    assert tags == {"Outdoor"}
    assert (await client.put(f"/api/users/{viewer.user_id}/restrictions", json={"tag_ids": [999999]})).status_code == 422

    # admins are never restricted, even with rules on file
    await app.state.pool.execute("UPDATE users SET role = 'admin' WHERE id = $1", viewer.user_id)
    assert (await viewer.get(f"/api/scenes/{hidden}")).status_code == 200


async def test_resume_position_is_per_user(client, login_as, seed):
    sid = seed["scenes"][2]
    other = await login_as("sam", "viewer")
    await client.put(f"/api/scenes/{sid}/resume", json={"time": 10})
    await other.put(f"/api/scenes/{sid}/resume", json={"time": 99})
    assert (await client.get(f"/api/scenes/{sid}")).json()["resume_time"] == 10
    assert (await other.get(f"/api/scenes/{sid}")).json()["resume_time"] == 99
    item = next(s for s in (await other.get("/api/scenes", params={"limit": 100})).json()["items"] if s["id"] == sid)
    assert item["resume_time"] == 99
    await other.put(f"/api/scenes/{sid}/resume", json={"time": None})
    assert (await other.get(f"/api/scenes/{sid}")).json()["resume_time"] is None


async def test_security_headers(client, anon):
    for c, path in ((client, "/api/scenes"), (anon, "/api/auth/status"), (anon, "/api/scenes")):
        r = await c.get(path)
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["cache-control"] == "no-store"


def test_throttle_backoff_caps():
    clock = [0.0]
    t = auth.LoginThrottle(clock=lambda: clock[0])
    key = ("1.2.3.4", "u")
    for _ in range(30):
        t.failure(key)
    assert t.retry_after(key) == auth.LoginThrottle.MAX_DELAY
    t.success(key)
    assert t.retry_after(key) == 0
