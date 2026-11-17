"""The worker's LISTEN connection comes back after the database drops it."""

import asyncio

from app import jobs, worker
from app.config import get_settings


async def test_the_listener_reconnects_and_hears_jobs_again(app):
    pool = app.state.pool
    wake, reload_requested, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(worker.listen_loop(get_settings().db_params(), wake, reload_requested, stop,
                                                  check_every=0.2, max_backoff=0.5))
    try:
        async def heard() -> bool:
            wake.clear()
            for _ in range(50):
                await pool.execute(f"NOTIFY {jobs.NOTIFY_CHANNEL}")
                if wake.is_set():
                    return True
                await asyncio.sleep(0.1)
            return False
        assert await heard()
        # the database drops the listening connection (as on a restart)
        killed = await pool.fetchval(
            "SELECT count(*) FILTER (WHERE pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE datname = current_database() AND application_name = $1", worker.LISTENER_APP_NAME)
        assert killed >= 1
        reload_requested.clear()
        for _ in range(50):                      # after reconnecting it asks the watcher to catch up
            if reload_requested.is_set():
                break
            await asyncio.sleep(0.1)
        assert reload_requested.is_set()
        assert await heard()
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
