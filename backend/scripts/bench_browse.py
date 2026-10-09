"""Benchmark the list API against a large synthetic library.

    docker compose run --rm goonstreamer-api python -m scripts.bench_browse [--scenes 200000]

Creates (and afterwards drops) a separate database, so real data is untouched.
"""

import argparse
import asyncio
import os
import statistics
import time

import asyncpg
from httpx import ASGITransport, AsyncClient

BENCH_DB = "goon_bench"

SEED_SQL = """
INSERT INTO library_paths (path) VALUES ('/media/bench');
INSERT INTO studios (name) SELECT 'Studio ' || g FROM generate_series(1, 200) g;
UPDATE studios SET parent_id = id - 100 WHERE id > 100;
INSERT INTO tags (name) SELECT 'Tag ' || g FROM generate_series(1, 500) g;
INSERT INTO tag_relations SELECT (g - 1) / 5 + 1, g FROM generate_series(6, 500) g;  -- 5-ary tree
INSERT INTO performers (name, gender, rating)
    SELECT 'Performer ' || g, (enum_range(NULL::gender))[1 + g % 6], g % 100 FROM generate_series(1, 2000) g;

INSERT INTO files (library_id, path, size, mod_time, oshash)
    SELECT 1, '/media/bench/scene' || g || '.mp4', g, now(), lpad(to_hex(g), 16, '0')
    FROM generate_series(1, {n}) g;
INSERT INTO scenes (file_id, title, duration, width, height, release_date, rating, play_count, studio_id, created_at)
    SELECT g, 'Scene ' || g, 60 + (g * 7919) % 5400, 1920, (ARRAY[480, 720, 1080, 2160])[1 + g % 4],
           date '2010-01-01' + (g * 31) % 5000, (g * 13) % 101, g % 5, 1 + g % 200,
           now() - make_interval(secs => g)
    FROM generate_series(1, {n}) g;
INSERT INTO scene_tags SELECT s, 1 + (s * k * 37) % 500 FROM generate_series(1, {n}) s, generate_series(1, 3) k
    ON CONFLICT DO NOTHING;
INSERT INTO scene_performers SELECT s, 1 + (s * k * 53) % 2000 FROM generate_series(1, {n}) s, generate_series(1, 2) k
    ON CONFLICT DO NOTHING;
INSERT INTO users (username, password_hash) VALUES ('restricted', 'x');
INSERT INTO user_hidden_tags VALUES (1, 2);          -- a subtree of ~100 tags
INSERT INTO user_hidden_studios VALUES (1, 7);
INSERT INTO user_hidden_performers VALUES (1, 11), (1, 12);
ANALYZE;
"""




async def timed(client, url, params, runs=15):
    times = []
    body = None
    for _ in range(runs):
        t = time.perf_counter()
        r = await client.get(url, params=params)
        times.append((time.perf_counter() - t) * 1000)
        assert r.status_code == 200, r.text
        body = r.json()
    return statistics.median(times), body


async def main(n: int, keep: bool) -> None:
    from app.config import get_settings
    admin = await asyncpg.connect(**get_settings().db_params("postgres"))
    await admin.execute(f"DROP DATABASE IF EXISTS {BENCH_DB} WITH (FORCE)")
    await admin.execute(f"CREATE DATABASE {BENCH_DB}")
    os.environ["POSTGRES_DB"] = BENCH_DB
    os.environ.pop("DATABASE_URL", None)
    get_settings.cache_clear()
    from app import auth
    from app.acl import load_acl
    from app.main import app, lifespan

    try:
        async with lifespan(app):
            pool = app.state.pool
            t = time.perf_counter()
            await pool.execute(SEED_SQL.format(n=n))
            print(f"seeded {n:,} scenes in {time.perf_counter() - t:.1f}s")
            from app import counts
            # user 1 ("restricted") comes from the seed; add an admin to browse unrestricted
            admin_id = await pool.fetchval(
                "INSERT INTO users (username, password_hash, role) VALUES ('bench-admin', 'x', 'admin') RETURNING id")

            async def signed_in(user_id: int) -> dict:
                return {"cookies": {auth.COOKIE_NAME: await auth.create_session(pool, user_id, "bench")},
                        "headers": {"X-Requested-With": "bench"}}

            t = time.perf_counter()
            viewers = await counts.refresh(pool)
            print(f"refresh_counts ({viewers} viewers) took {time.perf_counter() - t:.2f}s")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://bench",
                                   **await signed_in(admin_id)) as c:
                cases = [
                    ("newest, page 1", "/api/scenes", {}),
                    ("title, page 1", "/api/scenes", {"sort": "title", "desc": False}),
                    ("duration, page 1", "/api/scenes", {"sort": "duration"}),
                    ("tag filter (with sub-tags)", "/api/scenes", {"tag_ids": 1}),
                    ("2 tags, all", "/api/scenes", {"tag_ids": [3, 4]}),
                    ("performer filter", "/api/scenes", {"performer_ids": 5}),
                    ("studio tree filter", "/api/scenes", {"studio_ids": 3}),
                    ("4K only", "/api/scenes", {"height_min": 2160}),
                    ("title search", "/api/scenes", {"q": "scene 1999"}),
                    ("path search (one folder)", "/api/scenes", {"q": "bench/scene1234"}),
                    ("select-all ids for a tag", "/api/ids/scenes", {"tag_ids": 7}),
                    ("newest + total count", "/api/scenes", {"with_total": True}),
                    ("jump to the last page (offset)", "/api/scenes", {"offset": n - 60}),
                    ("jump to the middle, by title", "/api/scenes", {"offset": n // 2, "sort": "title", "desc": False}),
                    ("1000 per page", "/api/scenes", {"limit": 1000}),
                    ("select-all ids, everything (100k cap)", "/api/ids/scenes", {}),
                    ("grouped by performer, page 1", "/api/groups/scenes", {"limit": 20}),
                    ("grouped + tag filter", "/api/groups/scenes", {"limit": 20, "tag_ids": 3}),
                    ("grouped, sorted by title", "/api/groups/scenes", {"limit": 20, "sort": "title", "desc": False}),
                    ("performers (with counts)", "/api/performers", {}),
                    ("tags (with counts)", "/api/tags", {}),
                ]
                print(f"\n{'case':38} {'median':>9}")
                for label, url, params in cases:
                    ms, _ = await timed(c, url, {"limit": 60, **params})  # params may override limit
                    print(f"{label:38} {ms:8.1f}ms")

                # Deep pagination: walk forward to ~page 1000 and time a page there.
                cursor, pages = None, 0
                while pages < 1000:
                    r = (await c.get("/api/scenes", params={"limit": 60, **({"cursor": cursor} if cursor else {})})).json()
                    cursor, pages = r["next_cursor"], pages + 1
                ms, _ = await timed(c, "/api/scenes", {"limit": 60, "cursor": cursor})
                print(f"{'newest, page 1001 (row ~60,000)':38} {ms:8.1f}ms")

            acl = await load_acl(pool, 1)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://bench",
                                   **await signed_in(1)) as c:
                print(f"\nrestricted user: {len(acl.tags)} hidden tags, {len(acl.studios)} studios, "
                      f"{len(acl.performers)} performers")
                for label, params in [("newest, page 1", {}), ("title, page 1", {"sort": "title"}),
                                      ("newest + total count", {"with_total": True})]:
                    ms, body = await timed(c, "/api/scenes", {"limit": 60, **params})
                    extra = f"  (total {body['total']:,})" if body["total"] else ""
                    print(f"{'ACL ' + label:38} {ms:8.1f}ms{extra}")
                ms, _ = await timed(c, "/api/groups/scenes", {"limit": 20})
                print(f"{'ACL grouped by performer':38} {ms:8.1f}ms")
    finally:
        if not keep:
            await admin.execute(f"DROP DATABASE IF EXISTS {BENCH_DB} WITH (FORCE)")
        await admin.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", type=int, default=200_000)
    ap.add_argument("--keep", action="store_true", help="keep the database for inspection")
    args = ap.parse_args()
    asyncio.run(main(args.scenes, args.keep))
