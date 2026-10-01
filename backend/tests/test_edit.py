import io

import pytest
from PIL import Image

from app import counts
from app.acl import load_acl
from app.auth import get_acl
from tests.test_browse import seed  # noqa: F401  (fixture)


async def tag_names(client, path):
    return sorted(t["name"] for t in (await client.get(path)).json()["tags"])


# --- content --------------------------------------------------------------------

async def test_patch_scene_fields_and_links(app, client, seed):
    sid, t, p = seed["scenes"][1], seed["tags"], seed["performers"]
    r = await client.patch(f"/api/scenes/{sid}", json={
        "title": "Renamed", "rating": 90, "details": "notes", "urls": ["https://example.invalid/x"],
        "tag_ids": [t["Deep"], t["Outdoor"]], "performer_ids": [p["Carol"]], "studio_id": None,
        "custom_fields": {"source": "manual"},
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["title"], d["rating"], d["details"], d["studio"]) == ("Renamed", 90, "notes", None)
    assert sorted(x["name"] for x in d["tags"]) == ["Deep", "Outdoor"]
    assert [x["name"] for x in d["performers"]] == ["Carol"]
    assert d["custom_fields"] == {"source": "manual"}
    row = await app.state.pool.fetchrow("SELECT tag_ids, performer_ids FROM scenes WHERE id = $1", sid)
    assert sorted(row["tag_ids"]) == sorted([t["Deep"], t["Outdoor"]])  # arrays kept in sync
    assert list(row["performer_ids"]) == [p["Carol"]]

    # partial: only sent fields change
    d = (await client.patch(f"/api/scenes/{sid}", json={"organized": True})).json()
    assert d["title"] == "Renamed" and d["organized"] is True
    assert await app.state.pool.fetchval("SELECT count(*) FROM jobs WHERE kind = 'refresh_counts'") == 1


async def test_patch_validation(client, seed):
    sid = seed["scenes"][1]
    assert (await client.patch(f"/api/scenes/{sid}", json={"bogus": 1})).status_code == 422
    assert (await client.patch(f"/api/scenes/{sid}", json={"rating": 101})).status_code == 422
    assert (await client.patch(f"/api/scenes/{sid}", json={"tag_ids": None})).status_code == 422
    r = await client.patch(f"/api/scenes/{sid}", json={"tag_ids": [999999]})
    assert r.status_code == 422 and "does not exist" in r.text
    assert (await client.patch(f"/api/scenes/{sid}", json={"studio_id": 999999})).status_code == 422
    assert (await client.patch("/api/scenes/999999", json={"title": "x"})).status_code == 404
    img = seed["images"][0]
    assert (await client.patch(f"/api/images/{img}", json={"details": "x"})).status_code == 422
    assert (await client.patch(f"/api/images/{img}", json={"title": "pic"})).json()["title"] == "pic"


async def test_bulk_edit(app, client, seed):
    ids = [seed["scenes"][i] for i in (1, 3, 5)]
    t = seed["tags"]
    r = await client.post("/api/scenes/bulk", json={
        "ids": ids, "set": {"rating": 77}, "tags": {"mode": "add", "ids": [t["Deep"]]}})
    assert r.json() == {"updated": 3}
    for i in ids:
        d = (await client.get(f"/api/scenes/{i}")).json()
        assert d["rating"] == 77 and "Deep" in [x["name"] for x in d["tags"]]

    await client.post("/api/scenes/bulk", json={"ids": ids, "tags": {"mode": "remove", "ids": [t["Deep"]]}})
    assert all(["Deep" not in await tag_names(client, f"/api/scenes/{i}") for i in ids])

    await client.post("/api/scenes/bulk", json={"ids": ids, "tags": {"mode": "set", "ids": [t["Outdoor"]]}})
    assert all([await tag_names(client, f"/api/scenes/{i}") == ["Outdoor"] for i in ids])
    await client.post("/api/scenes/bulk", json={"ids": ids, "tags": {"mode": "set", "ids": []}})
    assert all([await tag_names(client, f"/api/scenes/{i}") == [] for i in ids])

    assert (await client.post("/api/scenes/bulk", json={"ids": [*ids, 999999]})).status_code == 404
    assert (await client.post("/api/scenes/bulk", json={"ids": []})).status_code == 422
    r = await client.post("/api/images/bulk", json={"ids": seed["images"], "set": {"organized": True}})
    assert r.json() == {"updated": 5}


async def test_writes_respect_acl(app, client, seed):
    pool = app.state.pool
    await pool.execute("INSERT INTO user_hidden_tags VALUES ($1, $2)", seed["user"], seed["tags"]["Standing"])
    acl = await load_acl(pool, seed["user"])
    app.dependency_overrides[get_acl] = lambda: acl
    try:
        hidden = seed["scenes"][0]
        assert (await client.patch(f"/api/scenes/{hidden}", json={"title": "x"})).status_code == 404
        r = await client.post("/api/scenes/bulk", json={"ids": [hidden, seed["scenes"][1]], "set": {"rating": 1}})
        assert r.status_code == 404
        assert await pool.fetchval("SELECT rating FROM scenes WHERE id = $1", seed["scenes"][1]) != 1
    finally:
        app.dependency_overrides.pop(get_acl)


async def test_play_and_resume(client, seed):
    sid = seed["scenes"][4]
    before = (await client.get(f"/api/scenes/{sid}")).json()["play_count"]
    assert (await client.post(f"/api/scenes/{sid}/play")).status_code == 204
    assert (await client.put(f"/api/scenes/{sid}/resume", json={"time": 42.5})).status_code == 204
    d = (await client.get(f"/api/scenes/{sid}")).json()
    assert d["play_count"] == before + 1 and d["resume_time"] == 42.5 and d["last_played_at"]
    assert (await client.put(f"/api/scenes/{sid}/resume", json={"time": -1})).status_code == 422


# --- performers / studios / tags ------------------------------------------------

async def test_performer_crud(app, client, seed):
    r = await client.post("/api/performers", json={
        "name": "Dana", "gender": "female", "height_cm": 170, "aliases": ["D", " D ", "Danny"],
        "tag_ids": [seed["tags"]["Outdoor"]]})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["aliases"] == ["D", "Danny"] and d["tags"][0]["name"] == "Outdoor" and d["height_cm"] == 170
    assert (await client.post("/api/performers", json={"name": "dana"})).status_code == 409
    assert (await client.post("/api/performers", json={"name": "Dana", "disambiguation": "2"})).status_code == 201
    assert (await client.post("/api/performers", json={"name": "X", "gender": "robot"})).status_code == 422

    d = (await client.patch(f"/api/performers/{d['id']}", json={"aliases": ["Dee"]})).json()
    assert d["aliases"] == ["Dee"] and d["favorite"] is False and d["gender"] == "female"
    assert (await client.patch(f"/api/performers/{d['id']}", json={"favorite": True})).status_code == 422  # personal now
    assert (await client.patch(f"/api/performers/{d['id']}", json={"name": None})).status_code == 422

    alice = seed["performers"]["Alice"]
    assert (await client.delete(f"/api/performers/{alice}")).status_code == 204
    assert (await client.get(f"/api/performers/{alice}")).status_code == 404
    assert await app.state.pool.fetchval("SELECT count(*) FROM scenes WHERE $1 = ANY(performer_ids)", alice) == 0
    assert await app.state.pool.fetchval("SELECT count(*) FROM scenes") == 25  # content untouched


async def test_studio_hierarchy_edits(app, client, seed):
    s = seed["studios"]
    r = await client.post("/api/studios", json={"name": "SubSub", "parent_id": s["sub"]})
    assert r.status_code == 201 and r.json()["parent"]["name"] == "Sub"
    subsub = r.json()["id"]
    r = await client.patch(f"/api/studios/{s['net']}", json={"parent_id": subsub})
    assert r.status_code == 409 and "cycle" in r.text
    assert (await client.delete(f"/api/studios/{s['sub']}")).status_code == 204
    assert (await client.get(f"/api/studios/{subsub}")).json()["parent"] is None  # detached, not deleted


async def test_tag_hierarchy_edits(client, seed):
    t = seed["tags"]
    r = await client.post("/api/tags", json={"name": "Kneeling", "parent_ids": [t["Position"]]})
    assert r.status_code == 201 and r.json()["parents"][0]["name"] == "Position"
    r = await client.patch(f"/api/tags/{t['Position']}", json={"parent_ids": [t["Deep"]]})
    assert r.status_code == 409  # Deep is a descendant of Position
    d = (await client.patch(f"/api/tags/{t['Deep']}", json={"parent_ids": [t["Position"]]})).json()
    assert [p["name"] for p in d["parents"]] == ["Position"]  # replaced Standing -> Position
    assert (await client.post("/api/tags", json={"name": "outdoor"})).status_code == 409


async def test_tag_merge(app, client, seed):
    t = seed["tags"]
    pool = app.state.pool
    await pool.execute("INSERT INTO user_hidden_tags VALUES ($1, $2)", seed["user"], t["Standing"])
    before = await pool.fetchval("SELECT count(*) FROM scenes WHERE tag_ids && $1::bigint[]",
                                 [t["Standing"], t["Outdoor"]])
    r = await client.post(f"/api/tags/{t['Outdoor']}/merge", json={"source_ids": [t["Standing"]]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [c["name"] for c in d["children"]] == ["Deep"]  # Standing's child moved over
    assert (await client.get(f"/api/tags/{t['Standing']}")).status_code == 404
    assert await pool.fetchval("SELECT count(*) FROM scenes WHERE $1 = ANY(tag_ids)", t["Outdoor"]) == before
    assert await pool.fetchval("SELECT tag_id FROM user_hidden_tags WHERE user_id = $1", seed["user"]) == t["Outdoor"]


async def test_tag_merge_skips_cycles(client, seed):
    t = seed["tags"]
    # merging Position (an ancestor of Deep) into Deep must not make Deep its own parent
    r = await client.post(f"/api/tags/{t['Deep']}/merge", json={"source_ids": [t["Position"]]})
    assert r.status_code == 200
    assert r.json()["children"] == []


# --- entity images --------------------------------------------------------------

def png_bytes(size=(2000, 1000)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, (255, 0, 0, 128)).save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.parametrize("kind", ["performers", "studios", "tags"])
async def test_entity_image_upload(client, seed, kind):
    eid = {"performers": seed["performers"]["Bob"], "studios": seed["studios"]["net"],
           "tags": seed["tags"]["Outdoor"]}[kind]
    assert (await client.get(f"/api/{kind}/{eid}/image")).status_code == 404
    r = await client.put(f"/api/{kind}/{eid}/image", content=png_bytes(), headers={"content-type": "image/png"})
    assert r.status_code == 204, r.text
    url = (await client.get(f"/api/{kind}/{eid}")).json()["image_url"]
    r = await client.get(url)
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    im = Image.open(io.BytesIO(r.content))
    assert im.format == "WEBP" and im.size == (1024, 512) and im.mode == "RGBA"
    assert (await client.put(f"/api/{kind}/{eid}/image", content=b"nope")).status_code == 422
    assert (await client.delete(f"/api/{kind}/{eid}/image")).status_code == 204
    assert (await client.get(f"/api/{kind}/{eid}")).json()["image_url"] is None


async def test_performer_image_in_lists(client, seed):
    bob = seed["performers"]["Bob"]
    await client.put(f"/api/performers/{bob}/image", content=png_bytes((10, 10)))
    items = {p["name"]: p for p in (await client.get("/api/performers")).json()["items"]}
    assert items["Bob"]["image_url"].startswith(f"/api/performers/{bob}/image?v=")
    scene = (await client.get(f"/api/scenes/{seed['scenes'][0]}")).json()
    assert scene["performers"][0]["image_url"] == items["Bob"]["image_url"]


async def test_entity_routes_do_not_shadow_others(client, seed):
    assert (await client.get("/api/scenes/1/image")).status_code == 404
    assert (await client.delete("/api/scenes/1")).status_code == 405
