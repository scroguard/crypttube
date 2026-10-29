import asyncpg
import pytest

from app.db import migrate


async def test_health(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_migrations_idempotent(app):
    assert await migrate(app.state.pool) == []


async def _tag(conn, name):
    return await conn.fetchval("INSERT INTO tags (name) VALUES ($1) RETURNING id", name)


async def test_tag_hierarchy_rejects_cycles(conn):
    a, b, c = [await _tag(conn, n) for n in ("a", "b", "c")]
    await conn.execute("INSERT INTO tag_relations VALUES ($1, $2), ($2, $3)", a, b, c)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("INSERT INTO tag_relations VALUES ($1, $2)", c, a)


async def test_tag_hierarchy_allows_dag(conn):
    root1, root2, child = [await _tag(conn, n) for n in ("r1", "r2", "child")]
    await conn.execute("INSERT INTO tag_relations VALUES ($1, $3), ($2, $3)", root1, root2, child)


async def test_tag_names_case_insensitive_unique(conn):
    await _tag(conn, "Outdoor")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _tag(conn, "outdoor")


async def test_studio_hierarchy_rejects_cycles(conn):
    net = await conn.fetchval("INSERT INTO studios (name) VALUES ('net') RETURNING id")
    sub = await conn.fetchval("INSERT INTO studios (name, parent_id) VALUES ('sub', $1) RETURNING id", net)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("UPDATE studios SET parent_id = $1 WHERE id = $2", sub, net)


async def test_gallery_image_count_maintained(conn):
    lib = await conn.fetchval("INSERT INTO library_paths (path) VALUES ('/media/x') RETURNING id")
    gallery = await conn.fetchval("INSERT INTO galleries (title) VALUES ('g') RETURNING id")
    for i in range(3):
        f = await conn.fetchval(
            "INSERT INTO files (library_id, path, size, mod_time) VALUES ($1, $2, 1, now()) RETURNING id",
            lib, f"/media/x/{i}.jpg",
        )
        img = await conn.fetchval("INSERT INTO images (file_id) VALUES ($1) RETURNING id", f)
        await conn.execute("INSERT INTO gallery_images VALUES ($1, $2, $3)", gallery, img, i)
    assert await conn.fetchval("SELECT image_count FROM galleries WHERE id = $1", gallery) == 3
    await conn.execute("DELETE FROM images WHERE id = $1", img)
    assert await conn.fetchval("SELECT image_count FROM galleries WHERE id = $1", gallery) == 2


async def test_updated_at_trigger(conn):
    tid = await _tag(conn, "t")
    before = await conn.fetchval("SELECT updated_at FROM tags WHERE id = $1", tid)
    # now() is fixed per transaction, so use clock_timestamp via a savepoint-free check:
    await conn.execute("UPDATE tags SET name = 't2', updated_at = '2000-01-01' WHERE id = $1", tid)
    after = await conn.fetchval("SELECT updated_at FROM tags WHERE id = $1", tid)
    assert after == before  # trigger overrode the explicit old timestamp with now()


async def test_link_arrays_follow_link_tables(conn):
    lib = await conn.fetchval("INSERT INTO library_paths (path) VALUES ('/media/arr') RETURNING id")
    scenes = []
    for i in range(2):
        f = await conn.fetchval(
            "INSERT INTO files (library_id, path, size, mod_time) VALUES ($1, $2, 1, now()) RETURNING id",
            lib, f"/media/arr/{i}.mp4")
        scenes.append(await conn.fetchval("INSERT INTO scenes (file_id) VALUES ($1) RETURNING id", f))
    a, b, c = [await _tag(conn, n) for n in ("ta", "tb", "tc")]

    async def arrays():
        return [list(r["tag_ids"]) for r in await conn.fetch(
            "SELECT tag_ids FROM scenes WHERE id = ANY($1) ORDER BY id", scenes)]

    # bulk insert across two parents in one statement
    await conn.execute("INSERT INTO scene_tags SELECT s, t FROM unnest($1::bigint[]) s, unnest($2::bigint[]) t",
                       scenes, [c, a])
    assert await arrays() == [sorted([a, c])] * 2
    await conn.execute("DELETE FROM scene_tags WHERE scene_id = $1 AND tag_id = $2", scenes[0], a)
    assert await arrays() == [[c], sorted([a, c])]
    await conn.execute("DELETE FROM tags WHERE id = $1", c)  # cascades through scene_tags
    assert await arrays() == [[], [a]]
    with pytest.raises(asyncpg.FeatureNotSupportedError):
        await conn.execute("UPDATE scene_tags SET tag_id = $1", b)


async def test_password_with_url_special_characters_connects(app):
    """Regression: the DSN used to be built by string interpolation, so a password
    containing @ # / ? : broke parsing (asyncpg then fell back to the OS username and
    failed with "No username set in the environment")."""
    from app.config import Settings, get_settings
    password = "p@ss#w/rd?:x%20 'q\"uote"
    pool = app.state.pool
    await pool.execute("DROP ROLE IF EXISTS special_pw_test")
    await pool.execute(f"CREATE ROLE special_pw_test LOGIN PASSWORD '{password.replace(chr(39), chr(39) * 2)}'")
    try:
        base = get_settings()
        s = Settings(postgres_host=base.postgres_host, postgres_port=base.postgres_port,
                     postgres_user="special_pw_test", postgres_password=password, postgres_db=base.postgres_db)
        conn = await asyncpg.connect(**s.db_params())
        try:
            assert await conn.fetchval("SELECT current_user") == "special_pw_test"
        finally:
            await conn.close()
    finally:
        await pool.execute("DROP ROLE special_pw_test")
