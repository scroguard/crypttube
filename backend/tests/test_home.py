"""Per-user favourites and the configurable home page."""

from app import counts
from tests.test_browse import seed  # noqa: F401  (fixture)
from tests.test_user_libraries import alice_lib, people  # noqa: F401  (fixtures)


# --- favourites -------------------------------------------------------------------

async def test_favourites_are_personal(client, login_as, seed):
    alice, bob = seed["performers"]["Alice"], seed["performers"]["Bob"]
    viewer = await login_as("favviewer", "viewer")

    assert (await viewer.put(f"/api/performers/{alice}/favorite", json={"favorite": True})).status_code == 204
    assert (await viewer.put(f"/api/performers/{alice}/favorite", json={"favorite": True})).status_code == 204  # idempotent
    assert (await client.put(f"/api/performers/{bob}/favorite", json={"favorite": True})).status_code == 204

    def names(body):
        return [p["name"] for p in body["items"]]

    assert names((await viewer.get("/api/performers", params={"favorite": True})).json()) == ["Alice"]
    assert names((await client.get("/api/performers", params={"favorite": True})).json()) == ["Bob"]
    assert names((await viewer.get("/api/performers", params={"favorite": False})).json()) == ["Bob", "Carol"]
    listed = {p["name"]: p["favorite"] for p in (await viewer.get("/api/performers")).json()["items"]}
    assert listed == {"Alice": True, "Bob": False, "Carol": False}
    assert (await viewer.get(f"/api/performers/{alice}")).json()["favorite"] is True
    assert (await client.get(f"/api/performers/{alice}")).json()["favorite"] is False

    assert (await viewer.put(f"/api/performers/{alice}/favorite", json={"favorite": False})).status_code == 204
    assert names((await viewer.get("/api/performers", params={"favorite": True})).json()) == []
    assert (await viewer.put("/api/performers/999999/favorite", json={"favorite": True})).status_code == 404


async def test_cannot_favourite_a_hidden_performer(app, client, login_as, seed):
    viewer = await login_as("favhidden", "viewer")
    bob = seed["performers"]["Bob"]
    await client.put(f"/api/users/{viewer.user_id}/restrictions", json={"performer_ids": [bob]})
    assert (await viewer.put(f"/api/performers/{bob}/favorite", json={"favorite": True})).status_code == 404


async def test_migration_copies_shared_favourites_to_every_user(app, clean):
    """The 0012 migration's data step, run against rows shaped like the old schema."""
    pool = app.state.pool
    async with pool.acquire() as c:
        tx = c.transaction()
        await tx.start()
        try:
            await c.execute("ALTER TABLE performers ADD COLUMN favorite boolean NOT NULL DEFAULT false")
            u1 = await c.fetchval("INSERT INTO users (username, password_hash) VALUES ('m1', 'x') RETURNING id")
            fav = await c.fetchval("INSERT INTO performers (name, favorite) VALUES ('Fav', true) RETURNING id")
            await c.execute("INSERT INTO performers (name) VALUES ('NotFav')")
            await c.execute("""INSERT INTO user_favorite_performers (user_id, performer_id)
                               SELECT u.id, p.id FROM users u CROSS JOIN performers p WHERE p.favorite""")
            rows = await c.fetch("SELECT user_id, performer_id FROM user_favorite_performers WHERE performer_id = $1", fav)
            assert (u1, fav) in {(r["user_id"], r["performer_id"]) for r in rows}
            assert await c.fetchval("SELECT count(DISTINCT performer_id) FROM user_favorite_performers") == 1
        finally:
            await tx.rollback()


# --- home layout ------------------------------------------------------------------

async def test_default_home_then_customize(client, login_as, seed):
    viewer = await login_as("homeviewer", "viewer")
    home = (await viewer.get("/api/me/home")).json()
    assert [s["type"] for s in home["sections"]] == ["continue_watching", "recent_scenes", "favorite_performers", "recent_galleries"]
    assert home["customized"] is False
    types = [a["type"] for a in home["available"]]
    assert "studios" in types and "my_uploads" not in types          # no upload right

    layout = {"sections": [{"type": "studios", "limit": 6}, {"type": "recent_scenes", "limit": 24, "title": "New stuff"}]}
    saved = (await viewer.put("/api/me/home", json=layout)).json()
    assert saved["customized"] is True
    assert saved["sections"] == [{"type": "studios", "limit": 6, "title": None},
                                 {"type": "recent_scenes", "limit": 24, "title": "New stuff"}]
    assert (await viewer.get("/api/me/home")).json()["sections"] == saved["sections"]
    # another user's home is unaffected
    assert (await client.get("/api/me/home")).json()["customized"] is False

    # an empty layout is a valid choice and is not replaced by the default
    assert (await viewer.put("/api/me/home", json={"sections": []})).json()["sections"] == []
    assert (await viewer.delete("/api/me/home")).json()["customized"] is False


async def test_home_validation(client, clean):
    for bad in ({"sections": [{"type": "nonsense"}]}, {"sections": [{"type": "studios", "limit": 0}]},
                {"sections": [{"type": "studios", "limit": 500}]}, {"sections": [{"type": "studios"}] * 21},
                {"sections": [{"type": "studios", "extra": 1}]}, {}):
        assert (await client.put("/api/me/home", json=bad)).status_code == 422, bad


async def test_my_uploads_section_follows_upload_right(app, people, alice_lib):
    alice, bob, admin = people["alice"], people["bob"], people["admin"]
    assert "my_uploads" in [a["type"] for a in (await alice.get("/api/me/home")).json()["available"]]
    await alice.put("/api/me/home", json={"sections": [{"type": "my_uploads"}, {"type": "recent_scenes"}]})
    assert [s["type"] for s in (await alice.get("/api/me/home")).json()["sections"]] == ["my_uploads", "recent_scenes"]
    # the admin switches uploads off: the section disappears instead of erroring
    await admin.patch(f"/api/users/{alice.user_id}", json={"can_upload": False})
    assert [s["type"] for s in (await alice.get("/api/me/home")).json()["sections"]] == ["recent_scenes"]
    assert bob


async def test_mine_filter(app, people, alice_lib, library_dir):
    from tests.test_user_libraries import drain
    alice, bob, admin = people["alice"], people["bob"], people["admin"]
    lib = (await admin.post("/api/libraries", json={"path": str(library_dir)})).json()
    await admin.post(f"/api/libraries/{lib['id']}/scan")
    await drain(app)
    everything = (await alice.get("/api/scenes")).json()["items"]
    mine = (await alice.get("/api/scenes", params={"mine": True})).json()["items"]
    assert len(everything) == 4 and [s["id"] for s in mine] == [alice_lib["scene"]]
    assert (await bob.get("/api/scenes", params={"mine": True})).json()["items"] == []
    assert len((await alice.get("/api/images", params={"mine": True})).json()["items"]) == 2
    assert len((await alice.get("/api/galleries", params={"mine": True})).json()["items"]) == 1


async def test_continue_watching_is_per_user_and_ordered(app, client, login_as, seed):
    viewer = await login_as("cwviewer", "viewer")
    a, b, c = seed["scenes"][1], seed["scenes"][5], seed["scenes"][7]
    for sid, t in ((a, 10), (b, 20)):
        await viewer.put(f"/api/scenes/{sid}/resume", json={"time": t})
    await client.put(f"/api/scenes/{c}/resume", json={"time": 30})            # someone else's progress
    await viewer.put(f"/api/scenes/{a}/resume", json={"time": 15})            # touched again -> first
    await app.state.pool.execute(
        "UPDATE user_scene_state SET updated_at = now() - interval '1 hour' WHERE user_id = $1 AND scene_id = $2",
        viewer.user_id, b)
    items = (await viewer.get("/api/me/continue-watching")).json()["items"]
    assert [s["id"] for s in items] == [a, b]
    assert items[0]["resume_time"] == 15 and "screenshot_url" in items[0]
    assert [s["id"] for s in (await client.get("/api/me/continue-watching")).json()["items"]] == [c]
    # finishing a video (resume cleared) removes it
    await viewer.put(f"/api/scenes/{a}/resume", json={"time": None})
    assert [s["id"] for s in (await viewer.get("/api/me/continue-watching")).json()["items"]] == [b]
    # hidden content never shows up, even with a saved position
    await client.put(f"/api/users/{viewer.user_id}/restrictions", json={"performer_ids": [seed["performers"]["Alice"]]})
    await counts.refresh(app.state.pool)
    assert (await viewer.get("/api/me/continue-watching")).json()["items"] == []   # scene 5 features Alice
