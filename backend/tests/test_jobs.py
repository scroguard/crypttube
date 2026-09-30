from app import jobs


async def test_dedupe_collapses_queued_jobs(app, clean):
    pool = app.state.pool
    a = await jobs.enqueue(pool, "scan", {"x": 1}, dedupe_key="k")
    b = await jobs.enqueue(pool, "scan", {"x": 1}, dedupe_key="k")
    assert a == b
    assert await pool.fetchval("SELECT count(*) FROM jobs") == 1


async def test_same_key_can_queue_while_running_but_not_run_concurrently(app, clean):
    pool = app.state.pool
    first = await jobs.enqueue(pool, "scan", dedupe_key="k")
    assert (await jobs.claim(pool))["id"] == first
    second = await jobs.enqueue(pool, "scan", dedupe_key="k")
    assert second != first
    assert await jobs.claim(pool) is None  # blocked by the running one
    await jobs.finish(pool, first)
    assert (await jobs.claim(pool))["id"] == second


async def test_delayed_jobs_wait_and_immediate_request_pulls_them_forward(app, clean):
    pool = app.state.pool
    jid = await jobs.enqueue(pool, "scan", dedupe_key="k", delay=3600)
    assert await jobs.claim(pool) is None
    assert await jobs.enqueue(pool, "scan", dedupe_key="k") == jid
    assert (await jobs.claim(pool))["id"] == jid


async def test_cancel(app, clean):
    pool = app.state.pool
    queued = await jobs.enqueue(pool, "scan")
    assert await jobs.cancel(pool, queued)
    assert await pool.fetchval("SELECT status FROM jobs WHERE id = $1", queued) == "cancelled"

    running = await jobs.enqueue(pool, "scan")
    await jobs.claim(pool)
    assert await jobs.cancel(pool, running)
    ctx = jobs.JobContext(pool, running, interval=0)
    assert await ctx.cancelled()
    await jobs.finish(pool, running)
    assert await pool.fetchval("SELECT status FROM jobs WHERE id = $1", running) == "cancelled"
    assert not await jobs.cancel(pool, running)


async def test_recover_stale(app, clean):
    pool = app.state.pool
    retry = await jobs.enqueue(pool, "scan", dedupe_key="a")
    await jobs.claim(pool)
    doomed = await jobs.enqueue(pool, "scan", dedupe_key="b")
    await pool.execute("UPDATE jobs SET status = 'running', attempts = $2 WHERE id = $1", doomed, jobs.MAX_ATTEMPTS)
    await jobs.recover_stale(pool)
    status = dict(await pool.fetch("SELECT id, status::text FROM jobs"))
    assert status == {retry: "queued", doomed: "failed"}


async def test_recover_stale_drops_superseded(app, clean):
    pool = app.state.pool
    old = await jobs.enqueue(pool, "scan", dedupe_key="a")
    await jobs.claim(pool)
    new = await jobs.enqueue(pool, "scan", dedupe_key="a")
    await jobs.recover_stale(pool)
    status = dict(await pool.fetch("SELECT id, status::text FROM jobs"))
    assert status == {old: "cancelled", new: "queued"}
