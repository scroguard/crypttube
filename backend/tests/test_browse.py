from datetime import date, timedelta

import pytest

from app import counts
from app.acl import UNRESTRICTED, load_acl
from app.auth import get_acl
from app.api.browse import GALLERY_SORTS, IMAGE_SORTS, SCENE_SORTS
from app.api.entities import PERFORMER_SORTS, STUDIO_SORTS, TAG_SORTS

N = 25
MISSING = 23


def scene_attrs(i: int) -> dict:
    return {
        "title": None if i in (3, 17) else f"Scene {i}",
        "duration": None if i % 7 == 0 else float(i * 10),
        "height": [480, 720, 1080][i % 3],
        "release_date": None if i % 4 == 0 else date(2020, 1, 1) + timedelta(days=(i * 7) % 30),
        "rating": None if i % 5 == 1 else (i * 4) % 100,
        "play_count": i % 4,
    }


@pytest.fixture
async def seed(app, clean):
    """25 scenes with varied attributes, a studio tree, a tag tree, performers, a gallery."""
    pool = app.state.pool
    async with pool.acquire() as c, c.transaction():
        lib = await c.fetchval("INSERT INTO library_paths (path) VALUES ('/media/seed') RETURNING id")
        net = await c.fetchval("INSERT INTO studios (name) VALUES ('Network') RETURNING id")
        sub = await c.fetchval("INSERT INTO studios (name, parent_id) VALUES ('Sub', $1) RETURNING id", net)
        other = await c.fetchval("INSERT INTO studios (name) VALUES ('Other') RETURNING id")
        tags = {}
        for name in ("Position", "Standing", "Deep", "Outdoor"):
            tags[name] = await c.fetchval("INSERT INTO tags (name) VALUES ($1) RETURNING id", name)
        await c.execute("INSERT INTO tag_relations VALUES ($1, $2), ($2, $3)",
                        tags["Position"], tags["Standing"], tags["Deep"])
        perf = {}
        for name in ("Alice", "Bob", "Carol"):
            perf[name] = await c.fetchval("INSERT INTO performers (name) VALUES ($1) RETURNING id", name)
        await c.execute("INSERT INTO performer_aliases VALUES ($1, 'Ally')", perf["Alice"])
        await c.execute("INSERT INTO performer_tags VALUES ($1, $2)", perf["Carol"], tags["Outdoor"])

        scenes = {}
        for i in range(N):
            a = scene_attrs(i)
            fid = await c.fetchval(
                """INSERT INTO files (library_id, path, size, mod_time, oshash, missing_since)
                   VALUES ($1, $2, 1, now(), $3, $4) RETURNING id""",
                lib, f"/media/seed/s{i}.mp4", f"{i:016x}", None if i != MISSING else date(2024, 1, 1))
            sid = await c.fetchval(
                """INSERT INTO scenes (file_id, title, duration, height, release_date, rating, play_count, studio_id)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id""",
                fid, a["title"], a["duration"], a["height"], a["release_date"], a["rating"], a["play_count"],
                [net, sub, other][i % 3])
            scenes[i] = sid
            if i % 2 == 0:
                await c.execute("INSERT INTO scene_tags VALUES ($1, $2)", sid, tags["Standing"])
            if i % 5 == 0:
                await c.execute("INSERT INTO scene_tags VALUES ($1, $2)", sid, tags["Outdoor"])
            if i % 10 == 0:
                await c.execute("INSERT INTO scene_tags VALUES ($1, $2)", sid, tags["Deep"])
            if i % 2 == 1:
                await c.execute("INSERT INTO scene_performers VALUES ($1, $2)", sid, perf["Alice"])
            if i % 3 == 0:
                await c.execute("INSERT INTO scene_performers VALUES ($1, $2)", sid, perf["Bob"])
        await c.execute("INSERT INTO scene_performers VALUES ($1, $2)", scenes[24], perf["Carol"])

        gallery = await c.fetchval("INSERT INTO galleries (title, folder_path) VALUES ('Pics', '/media/seed/pics') RETURNING id")
        images = []
        for i in range(5):
            fid = await c.fetchval(
                "INSERT INTO files (library_id, path, size, mod_time) VALUES ($1, $2, 1, now()) RETURNING id",
                lib, f"/media/seed/pics/{i}.jpg")
            img = await c.fetchval("INSERT INTO images (file_id, title, width, height) VALUES ($1, $2, 10, 20) RETURNING id",
                                   fid, f"img{i}")
            await c.execute("INSERT INTO gallery_images VALUES ($1, $2, $3)", gallery, img, 4 - i)
            images.append(img)
        await c.execute("UPDATE galleries SET cover_image_id = $2 WHERE id = $1", gallery, images[4])
        await c.execute("INSERT INTO image_tags VALUES ($1, $2)", images[0], tags["Outdoor"])
        user = await c.fetchval("INSERT INTO users (username, password_hash) VALUES ('seed-viewer', 'x') RETURNING id")
    await counts.refresh(pool)
    return {"scenes": scenes, "studios": {"net": net, "sub": sub, "other": other}, "tags": tags,
            "performers": perf, "gallery": gallery, "images": images, "user": user}


def idx(seed, ids):
    """Scene ids -> seed indexes, for readable assertions."""
    rev = {v: k for k, v in seed["scenes"].items()}
    return sorted(rev[i] for i in ids)


async def all_pages(client, url, **params):
    items, cursor, pages = [], None, 0
    while True:
        p = dict(params, **({"cursor": cursor} if cursor else {}))
        r = await client.get(url, params=p)
        assert r.status_code == 200, r.text
        body = r.json()
        items += body["items"]
        pages += 1
        assert pages < 50, "pagination did not terminate"
        cursor = body["next_cursor"]
        if not cursor:
            return items, pages


# --- pagination & sorting -------------------------------------------------------

SORT_KEYS = {
    "created_at": lambda i, a: 0,  # all seeded in one transaction: ties broken by id
    "title": lambda i, a: a["title"] or "",
    "duration": lambda i, a: a["duration"] if a["duration"] is not None else -1,
    "resolution": lambda i, a: a["height"],
    "release_date": lambda i, a: a["release_date"] or date(1, 1, 1),
    "rating": lambda i, a: a["rating"] if a["rating"] is not None else -1,
    "play_count": lambda i, a: a["play_count"],
}


def natural(s):
    import re
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


@pytest.mark.parametrize("sort", SORT_KEYS)
@pytest.mark.parametrize("desc", [False, True])
async def test_keyset_pagination_matches_full_sort(client, seed, sort, desc):
    items, pages = await all_pages(client, "/api/scenes", sort=sort, desc=desc, limit=7)
    present = [i for i in range(N) if i != MISSING]
    key = SORT_KEYS[sort]
    wrap = natural if sort == "title" else (lambda v: v)
    expected = sorted(present, key=lambda i: (wrap(key(i, scene_attrs(i))), seed["scenes"][i]), reverse=desc)
    assert [it["id"] for it in items] == [seed["scenes"][i] for i in expected]
    assert pages == 4  # 24 rows / 7 per page


async def test_cursor_validation(client, seed):
    body = (await client.get("/api/scenes", params={"limit": 2})).json()
    r = await client.get("/api/scenes", params={"cursor": body["next_cursor"], "sort": "title"})
    assert r.status_code == 422 and "different sort" in r.text
    assert (await client.get("/api/scenes", params={"cursor": "garbage!"})).status_code == 422
    assert (await client.get("/api/scenes", params={"sort": "nope"})).status_code == 422
    assert (await client.get("/api/scenes", params={"limit": 5000})).status_code == 422
    assert (await client.get("/api/scenes", params={"bogus": 1})).status_code == 422


async def test_total_is_optional(client, seed):
    assert (await client.get("/api/scenes", params={"limit": 5})).json()["total"] is None
    body = (await client.get("/api/scenes", params={"limit": 5, "with_total": True, "tag_ids": seed["tags"]["Outdoor"]})).json()
    assert body["total"] == 5  # 0, 5, 10, 15, 20


class CountingPool:
    def __init__(self, pool):
        self._pool, self.calls = pool, 0

    def __getattr__(self, name):
        attr = getattr(self._pool, name)
        if name in ("fetch", "fetchrow", "fetchval", "execute"):
            async def counted(*a, **kw):
                self.calls += 1
                return await attr(*a, **kw)
            return counted
        return attr


@pytest.mark.parametrize("url", ["/api/scenes", "/api/galleries", "/api/images",
                                 "/api/performers", "/api/studios", "/api/tags"])
async def test_lists_are_one_round_trip(app, client, seed, url):
    real = app.state.pool
    app.state.pool = counting = CountingPool(real)
    try:
        await client.get("/api/auth/me")
        auth_calls, counting.calls = counting.calls, 0  # session lookup, same for every route
        assert (await client.get(url, params={"limit": 50})).status_code == 200
        assert counting.calls == auth_calls + 1
        counting.calls = 0
        await client.get(url, params={"limit": 50, "with_total": True})
        assert counting.calls == auth_calls + 2
    finally:
        app.state.pool = real


ALL_SORTS = [("scenes s", SCENE_SORTS), ("galleries g", GALLERY_SORTS), ("images i", IMAGE_SORTS),
             ("performers p", PERFORMER_SORTS), ("studios st", STUDIO_SORTS), ("tags t", TAG_SORTS)]


@pytest.mark.parametrize("table,sorts", ALL_SORTS, ids=[t for t, _ in ALL_SORTS])
async def test_every_sort_has_a_matching_index(app, clean, table, sorts):
    alias = table.split()[1]
    async with app.state.pool.acquire() as c, c.transaction():
        await c.execute("SET LOCAL enable_seqscan = off; SET LOCAL enable_sort = off")
        for name, s in sorts.items():
            key = f"{s.expr} COLLATE {s.collate}" if s.collate else s.expr
            plan = "\n".join(r[0] for r in await c.fetch(
                f"EXPLAIN SELECT {alias}.id FROM {table} ORDER BY {key} DESC, {alias}.id DESC LIMIT 10"))
            assert "Index" in plan and "Sort" not in plan, f"{table} sort {name!r} has no usable index:\n{plan}"


# --- payload shape --------------------------------------------------------------

async def test_list_payload_is_small_and_includes_related(client, seed):
    body = (await client.get("/api/scenes", params={"sort": "title", "desc": False, "limit": 1, "q": "Scene 24"})).json()
    [item] = body["items"]
    assert item["studio"] == {"id": seed["studios"]["net"], "name": "Network"}
    assert [p["name"] for p in item["performers"]] == ["Bob", "Carol"]
    assert {t["name"] for t in item["tags"]} == {"Standing"}
    assert item["screenshot_url"].startswith(f"/api/scenes/{item['id']}/screenshot?w=640&v=")
    assert not {"details", "file", "custom_fields", "urls", "path"} & item.keys()


async def test_scene_detail(client, seed):
    sid = seed["scenes"][0]
    d = (await client.get(f"/api/scenes/{sid}")).json()
    assert d["file"]["path"] == "/media/seed/s0.mp4"
    assert d["studio"]["name"] == "Network"
    assert {t["name"] for t in d["tags"]} == {"Standing", "Outdoor", "Deep"}
    assert d["performers"][0]["image_url"] is None
    assert d["playback_url"] == f"/api/scenes/{sid}/playback"
    assert (await client.get("/api/scenes/999999")).status_code == 404


# --- filters --------------------------------------------------------------------

async def scene_ids(client, **params):
    items, _ = await all_pages(client, "/api/scenes", limit=100, **params)
    return [it["id"] for it in items]


async def test_tag_filters_follow_hierarchy(client, seed):
    t = seed["tags"]
    # Position has no direct scenes; its descendants Standing/Deep do
    assert idx(seed, await scene_ids(client, tag_ids=t["Position"])) == [i for i in range(0, N, 2) if i != MISSING]
    assert await scene_ids(client, tag_ids=t["Position"], include_sub_tags=False) == []
    both = idx(seed, await scene_ids(client, tag_ids=[t["Standing"], t["Outdoor"]]))
    assert both == [0, 10, 20]
    anyof = idx(seed, await scene_ids(client, tag_ids=[t["Deep"], t["Outdoor"]], tag_mode="any"))
    assert anyof == [0, 5, 10, 15, 20]
    excl = idx(seed, await scene_ids(client, exclude_tag_ids=t["Position"]))
    assert excl == [i for i in range(1, N, 2) if i != MISSING]


async def test_performer_and_studio_filters(client, seed):
    p, s = seed["performers"], seed["studios"]
    assert idx(seed, await scene_ids(client, performer_ids=[p["Alice"], p["Bob"]])) == [3, 9, 15, 21]
    assert idx(seed, await scene_ids(client, performer_ids=[p["Carol"], p["Bob"]], performer_mode="any")) == \
        [0, 3, 6, 9, 12, 15, 18, 21, 24]
    net_all = idx(seed, await scene_ids(client, studio_ids=s["net"]))
    assert net_all == [i for i in range(N) if i % 3 in (0, 1) and i != MISSING]  # includes Sub
    assert idx(seed, await scene_ids(client, studio_ids=s["net"], include_sub_studios=False)) == \
        [i for i in range(0, N, 3)]


async def test_scalar_filters(client, seed):
    got = idx(seed, await scene_ids(client, q="scene 1"))  # case-insensitive substring
    assert got == [1, 10, 11, 12, 13, 14, 15, 16, 18, 19]  # 17 has no title
    assert idx(seed, await scene_ids(client, q="100%")) == []  # LIKE wildcards are literal
    assert idx(seed, await scene_ids(client, height_min=1080)) == [i for i in range(2, N, 3) if i != MISSING]
    assert idx(seed, await scene_ids(client, duration_min=200, duration_max=220)) == [20, 22]
    assert idx(seed, await scene_ids(client, file_status="missing")) == [MISSING]
    assert len(await scene_ids(client, file_status="all")) == N
    rated = [i for i in range(N) if i != MISSING and (scene_attrs(i)["rating"] or -1) >= 80]
    assert idx(seed, await scene_ids(client, rating_min=80)) == rated
    assert idx(seed, await scene_ids(client, played=False)) == [i for i in range(0, N, 4)]


# --- galleries & images ---------------------------------------------------------

async def test_gallery_list_detail_and_images_in_order(client, seed):
    [g] = (await client.get("/api/galleries")).json()["items"]
    assert g["image_count"] == 5 and g["cover_url"].startswith(f"/api/images/{seed['images'][4]}/thumbnail?w=640&v=")
    d = (await client.get(f"/api/galleries/{seed['gallery']}")).json()
    assert d["folder_path"] == "/media/seed/pics"
    items, pages = await all_pages(client, f"/api/galleries/{seed['gallery']}/images", limit=2)
    assert [it["id"] for it in items] == list(reversed(seed["images"]))  # by position
    assert pages == 3
    assert items[0]["width"] == 10 and items[0]["file_url"].startswith("/api/images/")


async def test_image_detail(client, seed):
    d = (await client.get(f"/api/images/{seed['images'][0]}")).json()
    assert d["galleries"] == [{"id": seed["gallery"], "title": "Pics"}]
    assert d["tags"][0]["name"] == "Outdoor"


# --- entities -------------------------------------------------------------------

async def test_performers(client, seed):
    body = (await client.get("/api/performers")).json()
    assert [p["name"] for p in body["items"]] == ["Alice", "Bob", "Carol"]
    counts = {p["name"]: p["scene_count"] for p in body["items"]}
    assert counts == {"Alice": 11, "Bob": 9, "Carol": 1}  # Alice's scene 23 is missing
    assert [p["name"] for p in (await client.get("/api/performers", params={"q": "ally"})).json()["items"]] == ["Alice"]
    d = (await client.get(f"/api/performers/{seed['performers']['Alice']}")).json()
    assert d["aliases"] == ["Ally"] and d["scene_count"] == 11


async def test_studios_and_tags(client, seed):
    studios = {s["name"]: s for s in (await client.get("/api/studios")).json()["items"]}
    assert studios["Sub"]["parent"]["name"] == "Network"
    assert studios["Network"]["child_count"] == 1
    roots = (await client.get("/api/studios", params={"root_only": True})).json()["items"]
    assert {s["name"] for s in roots} == {"Network", "Other"}
    d = (await client.get(f"/api/studios/{seed['studios']['net']}")).json()
    assert [c["name"] for c in d["children"]] == ["Sub"]

    tags = {t["name"]: t for t in (await client.get("/api/tags")).json()["items"]}
    assert tags["Outdoor"]["scene_count"] == 5 and tags["Position"]["child_count"] == 1
    kids = (await client.get("/api/tags", params={"parent_id": seed["tags"]["Position"]})).json()["items"]
    assert [t["name"] for t in kids] == ["Standing"]
    d = (await client.get(f"/api/tags/{seed['tags']['Standing']}")).json()
    assert d["parents"][0]["name"] == "Position" and d["children"][0]["name"] == "Deep"
    assert d["performer_count"] == 0


# --- access control -------------------------------------------------------------

@pytest.fixture
async def restricted(app, seed):
    """Log in as a user who may not see the 'Position' tag tree, the 'Sub' studio, or Bob."""
    pool = app.state.pool
    u = seed["user"]
    await pool.execute("INSERT INTO user_hidden_tags VALUES ($1, $2)", u, seed["tags"]["Position"])
    await pool.execute("INSERT INTO user_hidden_studios VALUES ($1, $2)", u, seed["studios"]["sub"])
    await pool.execute("INSERT INTO user_hidden_performers VALUES ($1, $2)", u, seed["performers"]["Bob"])
    await counts.refresh(pool)
    acl = await load_acl(pool, u)
    app.dependency_overrides[get_acl] = lambda: acl
    yield acl
    app.dependency_overrides.pop(get_acl)


def visible_scenes():
    # hidden: Position tree (even i), Sub studio (i % 3 == 1), Bob (i % 3 == 0), missing
    return [i for i in range(N) if i % 2 == 1 and i % 3 == 2 and i != MISSING]


async def test_acl_expansion(seed, restricted):
    t = seed["tags"]
    assert set(restricted.tags) == {t["Position"], t["Standing"], t["Deep"]}
    assert restricted.studios == [seed["studios"]["sub"]]
    assert restricted.performers == [seed["performers"]["Bob"]]


async def test_acl_filters_lists_details_and_counts(client, seed, restricted):
    assert idx(seed, await scene_ids(client)) == visible_scenes()
    body = (await client.get("/api/scenes", params={"with_total": True, "limit": 1})).json()
    assert body["total"] == len(visible_scenes())
    hidden_scene = seed["scenes"][0]
    assert (await client.get(f"/api/scenes/{hidden_scene}")).status_code == 404

    tags = {t["name"]: t for t in (await client.get("/api/tags")).json()["items"]}
    assert set(tags) == {"Outdoor"}
    assert tags["Outdoor"]["scene_count"] == 1  # only 5 (odd, i%3==2) survives
    assert (await client.get(f"/api/tags/{seed['tags']['Deep']}")).status_code == 404

    performers = {p["name"]: p for p in (await client.get("/api/performers")).json()["items"]}
    assert set(performers) == {"Alice", "Carol"}
    assert performers["Alice"]["scene_count"] == len(visible_scenes())
    assert (await client.get(f"/api/performers/{seed['performers']['Bob']}")).status_code == 404

    studios = {s["name"]: s for s in (await client.get("/api/studios")).json()["items"]}
    assert set(studios) == {"Network", "Other"}
    assert studios["Network"]["child_count"] == 0


async def test_acl_hidden_tag_on_performer_hides_them_and_their_scenes(app, client, seed):
    pool = app.state.pool
    await pool.execute("INSERT INTO user_hidden_tags VALUES ($1, $2)", seed["user"], seed["tags"]["Outdoor"])
    acl = await load_acl(pool, seed["user"])
    assert seed["performers"]["Carol"] in acl.performers
    app.dependency_overrides[get_acl] = lambda: acl
    try:
        ids = idx(seed, await scene_ids(client))
        assert 24 not in ids and 5 not in ids and 1 in ids  # 24 has Carol, 5 is tagged Outdoor
    finally:
        app.dependency_overrides.pop(get_acl)


async def test_acl_images_in_hidden_gallery_are_hidden(app, client, seed):
    pool = app.state.pool
    await pool.execute("INSERT INTO gallery_tags VALUES ($1, $2)", seed["gallery"], seed["tags"]["Deep"])
    await pool.execute("INSERT INTO user_hidden_tags VALUES ($1, $2)", seed["user"], seed["tags"]["Position"])
    acl = await load_acl(pool, seed["user"])
    app.dependency_overrides[get_acl] = lambda: acl
    try:
        assert (await client.get("/api/galleries")).json()["items"] == []
        assert (await client.get("/api/images")).json()["items"] == []
        assert (await client.get(f"/api/images/{seed['images'][1]}")).status_code == 404
        assert (await client.get(f"/api/galleries/{seed['gallery']}/images")).status_code == 404
    finally:
        app.dependency_overrides.pop(get_acl)


async def test_unrestricted_default(seed):
    assert UNRESTRICTED.unrestricted


async def test_counts_cache_refresh(app, client, seed):
    pool = app.state.pool
    tag = seed["tags"]["Outdoor"]
    await pool.execute("INSERT INTO scene_tags VALUES ($1, $2)", seed["scenes"][1], tag)
    stale = (await client.get(f"/api/tags/{tag}")).json()["scene_count"]
    assert stale == 5  # cached until the refresh job runs
    await counts.refresh(pool)
    assert (await client.get(f"/api/tags/{tag}")).json()["scene_count"] == 6


async def test_scan_with_changes_requests_count_refresh(app, clean, library_dir):
    from app import worker
    from app.config import get_settings
    pool = app.state.pool
    lid = await pool.fetchval("INSERT INTO library_paths (path) VALUES ($1) RETURNING id", str(library_dir))
    await pool.execute("INSERT INTO jobs (kind, payload) VALUES ('scan', $1)", {"library_id": lid})
    await worker.run_one(pool, get_settings().model_copy(update={"scan_settle_seconds": 0}))
    kinds = {r["kind"] for r in await pool.fetch("SELECT kind FROM jobs WHERE status = 'queued'")}
    assert kinds == {"generate", "refresh_counts"}


# --- grouped by performer ---------------------------------------------------------

ALICE = [i for i in range(1, N, 2) if i != MISSING]             # odd
BOB = [i for i in range(0, N, 3)]                                # multiples of 3
UNASSIGNED = [i for i in range(N) if i % 2 == 0 and i % 3 != 0 and i != 24]


def group_view(seed, body):
    return {g["performer"]["name"]: (g["total"], idx(seed, [s["id"] for s in g["items"]])) for g in body["groups"]}


async def test_scenes_grouped_by_performer(client, seed):
    body = (await client.get("/api/groups/scenes", params={"per_group": 30})).json()
    assert [g["performer"]["name"] for g in body["groups"]] == ["Alice", "Bob", "Carol"]
    assert group_view(seed, body) == {"Alice": (len(ALICE), ALICE), "Bob": (len(BOB), BOB), "Carol": (1, [24])}
    assert body["next_cursor"] is None
    assert body["unassigned"]["total"] == len(UNASSIGNED)
    assert idx(seed, [s["id"] for s in body["unassigned"]["items"]]) == UNASSIGNED
    # a scene with two performers sits in both groups
    assert set(ALICE) & set(BOB) == {3, 9, 15, 21}
    # items have exactly the shape of the flat list
    flat = (await client.get("/api/scenes", params={"limit": 1})).json()["items"][0]
    assert body["groups"][0]["items"][0].keys() == flat.keys()


async def test_groups_limit_items_and_follow_the_sort(client, seed):
    body = (await client.get("/api/groups/scenes", params={"per_group": 3, "sort": "title", "desc": False})).json()
    alice = body["groups"][0]
    assert alice["total"] == len(ALICE) and len(alice["items"]) == 3
    assert [s["title"] for s in alice["items"]] == [None, None, "Scene 1"]        # untitled (3, 17) sort first
    newest = (await client.get("/api/groups/scenes", params={"per_group": 2})).json()["groups"][1]
    assert idx(seed, [s["id"] for s in newest["items"]]) == [21, 24]              # created_at desc -> highest ids
    assert (await client.get("/api/groups/scenes", params={"sort": "nope"})).status_code == 422


async def test_groups_paginate_and_unassigned_comes_last(client, seed):
    first = (await client.get("/api/groups/scenes", params={"limit": 2})).json()
    assert [g["performer"]["name"] for g in first["groups"]] == ["Alice", "Bob"]
    assert first["next_cursor"] and first["unassigned"] is None
    last = (await client.get("/api/groups/scenes", params={"limit": 2, "cursor": first["next_cursor"]})).json()
    assert [g["performer"]["name"] for g in last["groups"]] == ["Carol"]
    assert last["next_cursor"] is None and last["unassigned"]["total"] == len(UNASSIGNED)
    scene_cursor = (await client.get("/api/scenes", params={"limit": 2})).json()["next_cursor"]
    assert (await client.get("/api/groups/scenes", params={"cursor": scene_cursor})).status_code == 422


async def test_groups_respect_filters(client, seed):
    body = (await client.get("/api/groups/scenes", params={"tag_ids": seed["tags"]["Outdoor"], "per_group": 30})).json()
    assert group_view(seed, body) == {"Alice": (2, [5, 15]), "Bob": (2, [0, 15])}   # Carol has none: no group
    assert idx(seed, [s["id"] for s in body["unassigned"]["items"]]) == [10, 20]
    none = (await client.get("/api/groups/scenes", params={"q": "no such title"})).json()
    assert none == {"groups": [], "next_cursor": None, "unassigned": None}


async def test_no_performers_filter(client, seed):
    assert idx(seed, await scene_ids(client, no_performers=True)) == UNASSIGNED
    with_some = idx(seed, await scene_ids(client, no_performers=False))
    assert with_some == sorted(set(ALICE) | set(BOB) | {24})


async def test_groups_respect_acl(client, seed, restricted):
    body = (await client.get("/api/groups/scenes", params={"per_group": 30})).json()
    # Bob is hidden; Carol's only scene is hidden (so is her group); what's left is Alice's visible scenes
    assert group_view(seed, body) == {"Alice": (len(visible_scenes()), visible_scenes())}
    assert body["unassigned"] is None


async def test_images_grouped_by_performer(app, client, seed):
    body = (await client.get("/api/groups/images")).json()
    assert body["groups"] == [] and body["unassigned"]["total"] == 5
    assert body["unassigned"]["items"][0]["thumbnail_url"].startswith("/api/images/")
    await app.state.pool.execute("INSERT INTO image_performers VALUES ($1, $2), ($3, $2)",
                                 seed["images"][0], seed["performers"]["Carol"], seed["images"][1])
    body = (await client.get("/api/groups/images")).json()
    [g] = body["groups"]
    assert (g["performer"]["name"], g["total"]) == ("Carol", 2)
    assert sorted(i["id"] for i in g["items"]) == sorted(seed["images"][:2])
    assert body["unassigned"]["total"] == 3


async def test_grouped_page_uses_a_fixed_number_of_queries(app, client, seed):
    real = app.state.pool
    app.state.pool = counting = CountingPool(real)
    try:
        await client.get("/api/auth/me")
        auth_calls, counting.calls = counting.calls, 0
        await client.get("/api/groups/scenes", params={"limit": 2})          # more pages follow
        assert counting.calls == auth_calls + 3                              # probe + groups + their items
        counting.calls = 0
        await client.get("/api/groups/scenes")                               # last page adds "unassigned"
        assert counting.calls == auth_calls + 4
    finally:
        app.state.pool = real


async def test_both_grouping_strategies_agree(client, seed, monkeypatch):
    """Few matches are grouped from the items, many by walking performers; same answer."""
    from app.api import groups
    params = {"per_group": 4, "limit": 2, "sort": "title", "desc": False}
    results = []
    for threshold in (5000, 0):          # sparse strategy, then dense strategy
        monkeypatch.setattr(groups, "SPARSE_LIMIT", threshold)
        pages, cursor = [], None
        while True:
            body = (await client.get("/api/groups/scenes", params={**params, **({"cursor": cursor} if cursor else {})})).json()
            pages.append(body)
            cursor = body["next_cursor"]
            if not cursor:
                break
        results.append(pages)
    assert results[0] == results[1]
    assert [g["performer"]["name"] for p in results[0] for g in p["groups"]] == ["Alice", "Bob", "Carol"]


# --- search by path, and "select all matching" ------------------------------------

async def test_search_matches_title_or_path(app, client, seed):
    pool = app.state.pool
    # put three scenes under a performer-named folder; titles say nothing about it
    for i in (2, 4, 6):
        await pool.execute("UPDATE files SET path = $2 WHERE id = (SELECT file_id FROM scenes WHERE id = $1)",
                           seed["scenes"][i], f"/media/seed/Jasmine Lane/clip{i}.mp4")
    assert idx(seed, await scene_ids(client, q="jasmine")) == [2, 4, 6]            # by folder, case-insensitive
    assert idx(seed, await scene_ids(client, q="Jasmine Lane/clip4")) == [4]
    assert idx(seed, await scene_ids(client, q="Scene 12")) == [12]                # still by title
    assert idx(seed, await scene_ids(client, q="seed/s1")) == [1, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]
    # combines with other filters, and with grouping
    assert idx(seed, await scene_ids(client, q="jasmine", tag_ids=seed["tags"]["Standing"])) == [2, 4, 6]
    body = (await client.get("/api/groups/scenes", params={"q": "jasmine"})).json()
    assert [g["performer"]["name"] for g in body["groups"]] == ["Bob"]             # scene 6
    assert idx(seed, [s["id"] for s in body["unassigned"]["items"]]) == [2, 4]


async def test_search_images_and_galleries_by_path(app, client, seed):
    assert len((await client.get("/api/images", params={"q": "seed/pics"})).json()["items"]) == 5
    assert [i["title"] for i in (await client.get("/api/images", params={"q": "pics/3"})).json()["items"]] == ["img3"]
    assert (await client.get("/api/images", params={"q": "nowhere"})).json()["items"] == []
    assert [g["title"] for g in (await client.get("/api/galleries", params={"q": "seed/pics"})).json()["items"]] == ["Pics"]
    assert [g["title"] for g in (await client.get("/api/galleries", params={"q": "pic"})).json()["items"]] == ["Pics"]


async def test_ids_of_everything_matching(app, client, login_as, seed):
    present = [i for i in range(N) if i != MISSING]
    body = (await client.get("/api/ids/scenes")).json()
    assert idx(seed, body["ids"]) == present and body["truncated"] is False
    body = (await client.get("/api/ids/scenes", params={"tag_ids": seed["tags"]["Outdoor"]})).json()
    assert idx(seed, body["ids"]) == [0, 5, 10, 15, 20]
    imgs = (await client.get("/api/ids/images", params={"gallery_id": seed["gallery"]})).json()
    assert sorted(imgs["ids"]) == sorted(seed["images"])
    assert (await client.get("/api/ids/images", params={"gallery_id": 999999})).json()["ids"] == []

    # only what the caller may edit: a viewer can edit nothing in a global library
    viewer = await login_as("idsviewer", "viewer")
    assert (await viewer.get("/api/ids/scenes")).json() == {"ids": [], "truncated": False, "limit": 100_000,
                                                            "skipped": len(present)}
    editor = await login_as("idseditor", "editor")
    body = (await editor.get("/api/ids/scenes")).json()
    assert len(body["ids"]) == len(present) and body["skipped"] == 0
    # and the ids can be bulk-edited in one go
    ids = (await editor.get("/api/ids/scenes", params={"q": "Scene 1"})).json()["ids"]
    r = await editor.post("/api/scenes/bulk", json={"ids": ids, "performers": {"mode": "add", "ids": [seed["performers"]["Carol"]]}})
    assert r.json() == {"updated": len(ids)}


async def test_select_all_is_capped(app, client, seed, monkeypatch):
    from app.api import browse
    monkeypatch.setattr(browse, "SELECT_ALL_LIMIT", 5)
    body = (await client.get("/api/ids/scenes")).json()
    assert len(body["ids"]) == 5 and body["truncated"] is True and body["skipped"] == 0


async def test_offset_jumps_to_a_page(client, seed):
    """Paged browsing can jump to any page; it lines up exactly with cursor paging."""
    walked, cursor = [], None
    while True:
        r = (await client.get("/api/scenes", params={"limit": 7, "sort": "title", "desc": False, "cursor": cursor})).json()
        walked.append([s["id"] for s in r["items"]])
        cursor = r["next_cursor"]
        if not cursor:
            break
    for n, expected in enumerate(walked):
        r = (await client.get("/api/scenes", params={"limit": 7, "sort": "title", "desc": False, "offset": n * 7})).json()
        assert [s["id"] for s in r["items"]] == expected
        assert (r["next_cursor"] is None) == (n == len(walked) - 1)
    past = (await client.get("/api/scenes", params={"limit": 7, "offset": 7000})).json()
    assert past["items"] == [] and past["next_cursor"] is None
    assert (await client.get("/api/scenes", params={"limit": 1000})).status_code == 200
    assert (await client.get("/api/scenes", params={"limit": 1001})).status_code == 422
    assert (await client.get("/api/performers", params={"limit": 1, "offset": 1})).json()["items"][0]["name"]


async def test_search_matches_performer_and_studio_names_within_a_scope(app, client, clean, library_dir):
    """Searching "katie" finds Katie's scenes even when no title or file says so; on a
    studio's page the same search stays within that studio."""
    from app.scanner import Scanner
    from app.config import get_settings
    pool = app.state.pool
    lid = await pool.fetchval("INSERT INTO library_paths (path) VALUES ($1) RETURNING id", str(library_dir))
    await Scanner(pool, get_settings().generated_dir, settle_seconds=0).scan(lid)
    scenes = [r["id"] for r in await pool.fetch("SELECT id FROM scenes ORDER BY id")]
    images = [r["id"] for r in await pool.fetch("SELECT id FROM images ORDER BY id")]
    katie = (await client.post("/api/performers", json={"name": "Katie Smith", "aliases": ["KS Doll"]})).json()["id"]
    abc = (await client.post("/api/studios", json={"name": "ABC Studios"})).json()["id"]
    xyz = (await client.post("/api/studios", json={"name": "XYZ Media"})).json()["id"]
    await client.patch(f"/api/scenes/{scenes[0]}", json={"performer_ids": [katie], "studio_id": abc})
    await client.patch(f"/api/scenes/{scenes[1]}", json={"performer_ids": [katie], "studio_id": xyz})
    await client.patch(f"/api/scenes/{scenes[2]}", json={"studio_id": abc})
    await client.patch(f"/api/images/{images[0]}", json={"performer_ids": [katie], "studio_id": abc})

    ids = lambda r: {i["id"] for i in r.json()["items"]}  # noqa: E731
    # everywhere (the main Scenes page)
    assert ids(await client.get("/api/scenes", params={"q": "katie"})) == {scenes[0], scenes[1]}
    assert ids(await client.get("/api/scenes", params={"q": "ks doll"})) == {scenes[0], scenes[1]}   # alias
    assert ids(await client.get("/api/scenes", params={"q": "xyz"})) == {scenes[1]}                  # studio name
    # on ABC Studios' page: only ABC's
    assert ids(await client.get("/api/scenes", params={"q": "katie", "studio_ids": abc})) == {scenes[0]}
    # on Katie's page: a studio name narrows her scenes
    assert ids(await client.get("/api/scenes", params={"q": "abc", "performer_ids": katie})) == {scenes[0]}
    assert ids(await client.get("/api/images", params={"q": "katie", "studio_ids": abc})) == {images[0]}
    assert ids(await client.get("/api/images", params={"q": "katie", "studio_ids": xyz})) == set()
    # "select all matching" follows the same rules
    assert set((await client.get("/api/ids/scenes", params={"q": "katie", "studio_ids": abc})).json()["ids"]) == {scenes[0]}
