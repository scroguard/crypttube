from app import jobs
from tests.test_media_api import indexed  # noqa: F401  (fixture)


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


# --- running jobs side by side ---------------------------------------------------------

async def test_generate_jobs_run_in_parallel_and_scans_alone(app, clean, monkeypatch):
    """Up to N generate jobs run at once; a scan never overlaps anything heavy, and a
    waiting scan goes before further generate jobs."""
    import asyncio
    from app import maintenance, worker
    from app.config import get_settings
    pool = app.state.pool
    log: list[tuple[str, str, int]] = []
    now = {"generate": 0, "scan": 0, "max_generate": 0, "overlap": 0}

    def fake(kind, seconds):
        async def handler(pool, settings, ctx, payload):
            now[kind] += 1
            now["max_generate"] = max(now["max_generate"], now["generate"])
            if now["scan"] and now["generate"]:
                now["overlap"] += 1
            log.append(("start", kind, payload["n"]))
            await asyncio.sleep(seconds)
            log.append(("end", kind, payload["n"]))
            now[kind] -= 1
            return "ok"
        return handler

    monkeypatch.setitem(worker.HANDLERS, "generate", fake("generate", 0.3))
    monkeypatch.setitem(worker.HANDLERS, "scan", fake("scan", 0.2))
    await maintenance.set_state(pool, worker.WORKER_SETTINGS, {"generate_jobs": 3})
    for n in range(5):
        await pool.execute("INSERT INTO jobs (kind, payload) VALUES ('generate', $1)", {"n": n})
    await pool.execute("INSERT INTO jobs (kind, payload, run_after) VALUES ('scan', $1, now() + interval '0.15 seconds')", {"n": 99})
    for n in range(5, 7):
        await pool.execute("INSERT INTO jobs (kind, payload, run_after) VALUES ('generate', $1, now() + interval '0.2 seconds')", {"n": n})

    stop, wake = asyncio.Event(), asyncio.Event()
    loop = asyncio.create_task(worker.job_loop(pool, get_settings(), wake, stop))
    for _ in range(100):
        await asyncio.sleep(0.1)
        wake.set()          # (no NOTIFY listener here: poke the loop like a notification would)
        if not await pool.fetchval("SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')"):
            break
    stop.set()
    await loop

    assert await pool.fetchval("SELECT count(*) FROM jobs WHERE status = 'done'") == 8
    assert now["max_generate"] == 3 and now["overlap"] == 0
    events = [(e, n) for e, _, n in log]
    scan_start, scan_end = events.index(("start", 99)), events.index(("end", 99))
    # the scan waited for the running generate jobs, and the later ones waited for the scan
    assert all(("end", n) in events[:scan_start] for n in range(3))
    assert all(events.index(("start", n)) > scan_end for n in (5, 6))


async def test_worker_settings_api(app, client, clean, login_as):
    assert (await client.get("/api/jobs/settings")).json() == {"generate_jobs": 1, "max_generate_jobs": 16}
    r = await client.put("/api/jobs/settings", json={"generate_jobs": 4})
    assert r.status_code == 200 and r.json()["generate_jobs"] == 4
    assert (await client.put("/api/jobs/settings", json={"generate_jobs": 0})).status_code == 422
    assert (await client.put("/api/jobs/settings", json={"generate_jobs": 17})).status_code == 422
    viewer = await login_as("vera", "viewer")
    assert (await viewer.put("/api/jobs/settings", json={"generate_jobs": 2})).status_code == 403


async def test_large_generate_is_split_into_batches(app, client, indexed, monkeypatch):
    from app import worker
    from app.config import get_settings
    from app.jobs import JobContext
    pool = app.state.pool
    monkeypatch.setattr(worker, "SCENE_BATCH", 2)
    monkeypatch.setattr(worker, "IMAGE_BATCH", 4)
    job_id = await pool.fetchval("INSERT INTO jobs (kind, payload, status) VALUES ('generate', '{}', 'running') RETURNING id")
    msg = await worker.handle_generate(pool, get_settings(), JobContext(pool, job_id), {"missing": True})
    assert msg == "queued 4 batches"          # 3 scenes in 2s, 6 images in 4s
    batches = [r["payload"] for r in await pool.fetch("SELECT payload FROM jobs WHERE status = 'queued' ORDER BY id")]
    assert sorted(len(b["scene_ids"]) for b in batches if b["scene_ids"]) == [1, 2]
    assert sorted(len(b["image_ids"]) for b in batches if b["image_ids"]) == [2, 4]
    while await worker.run_one(pool, get_settings()):
        pass
    assert await pool.fetchval("SELECT count(*) FROM jobs WHERE status <> 'done' AND kind = 'generate'") == 1  # the parent, still 'running' here
    for sid in indexed["scenes"].values():
        assert (await client.get(f"/api/scenes/{sid}/sprite.vtt")).status_code == 200



async def test_job_list_can_hide_batches(app, client, clean):
    pool = app.state.pool
    for kind, payload in (("scan", {}), ("generate", {"force": True}), ("generate", {"scene_ids": [1], "image_ids": []}),
                          ("refresh_counts", {}), ("delete_duplicates", {"kind": "scene"})):
        await pool.execute("INSERT INTO jobs (kind, payload) VALUES ($1, $2)", kind, payload)
    every = [j["kind"] for j in (await client.get("/api/jobs")).json()["items"]]
    assert len(every) == 5
    shown = [(j["kind"], j["payload"]) for j in (await client.get("/api/jobs", params={"hide_batches": True})).json()["items"]]
    assert shown == [("delete_duplicates", {"kind": "scene"}), ("generate", {"force": True}), ("scan", {})]
