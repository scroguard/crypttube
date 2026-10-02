"""Users' own content filters (tags and performers they don't want to see)."""

from app import counts
from tests.test_media_api import indexed  # noqa: F401  (fixture)


async def setup(app, client, indexed):
    """h264: tag Outdoor>Beach + performer Ann; hevc: performer Bob; vp9: nothing."""
    mk = lambda path, **body: client.post(path, json=body)  # noqa: E731
    outdoor = (await mk("/api/tags", name="Outdoor")).json()["id"]
    beach = (await mk("/api/tags", name="Beach", parent_ids=[outdoor])).json()["id"]
    ann = (await mk("/api/performers", name="Ann")).json()["id"]
    bob = (await mk("/api/performers", name="Bob")).json()["id"]
    s = indexed["scenes"]
    await client.patch(f"/api/scenes/{s['h264']}", json={"tag_ids": [beach], "performer_ids": [ann]})
    await client.patch(f"/api/scenes/{s['hevc']}", json={"performer_ids": [bob]})
    await counts.refresh(app.state.pool)
    return {"outdoor": outdoor, "beach": beach, "ann": ann, "bob": bob}


async def scene_ids(c) -> set[int]:
    return {s["id"] for s in (await c.get("/api/scenes")).json()["items"]}


async def test_filters_hide_content_for_that_user_only(app, client, indexed, login_as):
    ids = await setup(app, client, indexed)
    s = indexed["scenes"]
    vera, other = await login_as("vera", "viewer"), await login_as("otto", "viewer")
    assert (await vera.get("/api/me/filters")).json() == {"enabled": True, "tags": [], "performers": []}
    assert await scene_ids(vera) == set(s.values())

    # a parent tag: hides what is tagged with the tags under it
    r = await vera.put("/api/me/filters", json={"tag_ids": [ids["outdoor"]], "performer_ids": [ids["bob"]]})
    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": True, "tags": [{"id": ids["outdoor"], "name": "Outdoor"}],
                        "performers": [{"id": ids["bob"], "name": "Bob"}]}
    assert await scene_ids(vera) == {s["vp9"]}
    assert (await vera.get(f"/api/scenes/{s['h264']}")).status_code == 404
    assert (await vera.get(f"/api/scenes/{s['h264']}/stream")).status_code == 404
    assert (await vera.get(f"/api/performers/{ids['bob']}")).status_code == 404
    names = lambda r: {x["name"] for x in r.json()["items"]}  # noqa: E731
    assert names(await vera.get("/api/tags")) == set()
    # Ann is not filtered, but her only scene is: she stays, with nothing to show (counts are immediate)
    perf = {p["name"]: p for p in (await vera.get("/api/performers")).json()["items"]}
    assert set(perf) == {"Ann"} and perf["Ann"]["scene_count"] == 0
    # nobody else is affected
    assert await scene_ids(other) == set(s.values()) and await scene_ids(client) == set(s.values())
    assert names(await other.get("/api/tags")) == {"Outdoor", "Beach"}

    # switched off: everything is back, and the selection is remembered
    r = await vera.put("/api/me/filters", json={"enabled": False, "tag_ids": [ids["outdoor"]], "performer_ids": [ids["bob"]]})
    assert r.json()["enabled"] is False and len(r.json()["tags"]) == 1
    assert await scene_ids(vera) == set(s.values())
    perf = {p["name"]: p for p in (await vera.get("/api/performers")).json()["items"]}
    assert perf["Ann"]["scene_count"] == 1 and "Bob" in perf
    # back on, then cleared
    await vera.put("/api/me/filters", json={"enabled": True, "tag_ids": [], "performer_ids": [ids["ann"]]})
    assert await scene_ids(vera) == {s["hevc"], s["vp9"]}
    await vera.put("/api/me/filters", json={})
    assert await scene_ids(vera) == set(s.values())


async def test_admins_can_filter_too(app, client, indexed):
    ids = await setup(app, client, indexed)
    s = indexed["scenes"]
    await client.put("/api/me/filters", json={"performer_ids": [ids["ann"]]})
    try:
        assert await scene_ids(client) == {s["hevc"], s["vp9"]}
        assert (await client.get("/api/me/filters")).json()["performers"] == [{"id": ids["ann"], "name": "Ann"}]
    finally:
        await client.put("/api/me/filters", json={})
    assert await scene_ids(client) == set(s.values())


async def test_filters_cannot_reveal_restricted_names(app, client, indexed, login_as):
    """A user can't use the filter list to learn the name of something hidden from them."""
    ids = await setup(app, client, indexed)
    vera = await login_as("vera", "viewer")
    await client.put(f"/api/users/{vera.user_id}/restrictions", json={"performer_ids": [ids["bob"]]})
    r = await vera.put("/api/me/filters", json={"performer_ids": [ids["bob"]]})
    assert r.status_code == 422
    assert (await vera.put("/api/me/filters", json={"tag_ids": [999999]})).status_code == 422
    # chosen first, restricted by the admin later: no longer listed
    await vera.put("/api/me/filters", json={"performer_ids": [ids["ann"]]})
    await client.put(f"/api/users/{vera.user_id}/restrictions", json={"performer_ids": [ids["ann"]]})
    assert (await vera.get("/api/me/filters")).json()["performers"] == []


async def test_filters_follow_deleted_and_merged_tags(app, client, indexed, login_as):
    ids = await setup(app, client, indexed)
    vera = await login_as("vera", "viewer")
    await vera.put("/api/me/filters", json={"tag_ids": [ids["beach"]], "performer_ids": [ids["bob"]]})
    sand = (await client.post("/api/tags", json={"name": "Sand"})).json()["id"]
    assert (await client.post(f"/api/tags/{sand}/merge", json={"source_ids": [ids["beach"]]})).status_code == 200
    assert (await client.delete(f"/api/performers/{ids['bob']}")).status_code == 204
    body = (await vera.get("/api/me/filters")).json()
    assert [t["name"] for t in body["tags"]] == ["Sand"] and body["performers"] == []
    assert indexed["scenes"]["h264"] not in await scene_ids(vera)


async def test_requires_sign_in(anon):
    assert (await anon.get("/api/me/filters")).status_code == 401
