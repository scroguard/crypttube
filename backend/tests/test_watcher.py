from watchdog.events import (DirCreatedEvent, DirDeletedEvent, DirModifiedEvent, FileCreatedEvent,
                             FileModifiedEvent, FileMovedEvent)

from app.watcher import Debouncer, MAX_DIRS_PER_LIBRARY, LibraryWatcher, _Handler, collapse


def test_collapse_keeps_topmost():
    assert collapse({"/m/a", "/m/a/b", "/m/a/b/c", "/m/ab", "/m/c"}) == ["/m/a", "/m/ab", "/m/c"]


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_debouncer_waits_for_quiet():
    clock = Clock()
    d = Debouncer(quiet_seconds=5, max_wait_seconds=60, clock=clock)
    d.add(1, "/m/a")
    clock.t = 3
    d.add(1, "/m/a/b")
    clock.t = 7
    assert d.pop_ready() == {}  # only 4s since last event
    clock.t = 8
    assert d.pop_ready() == {1: ["/m/a"]}
    assert d.pop_ready() == {}


def test_debouncer_max_wait_prevents_starvation():
    clock = Clock()
    d = Debouncer(quiet_seconds=5, max_wait_seconds=20, clock=clock)
    for t in range(0, 25, 2):
        clock.t = t
        d.add(1, "/m/busy")
        if d.pop_ready():
            assert t >= 20
            return
    raise AssertionError("never flushed")


def events_for(*evts):
    got = []
    h = _Handler(7, "/m/lib", lambda lib, d: got.append((lib, d)))
    for e in evts:
        h.on_any_event(e)
    return got


def test_handler_maps_events_to_directories():
    assert events_for(FileCreatedEvent("/m/lib/x/a.mp4")) == [(7, "/m/lib/x")]
    assert events_for(DirCreatedEvent("/m/lib/x/new")) == [(7, "/m/lib/x/new")]
    assert events_for(DirDeletedEvent("/m/lib/x/old")) == [(7, "/m/lib/x")]
    assert events_for(FileMovedEvent("/m/lib/a/v.mp4", "/m/lib/b/v.mp4")) == [(7, "/m/lib/a"), (7, "/m/lib/b")]
    assert events_for(DirDeletedEvent("/m/lib")) == [(7, "/m/lib")]


def test_handler_ignores_directory_modified():
    assert events_for(DirModifiedEvent("/m/lib")) == []


def test_handler_ignores_irrelevant_files():
    assert events_for(FileModifiedEvent("/m/lib/notes.txt")) == []
    assert events_for(FileCreatedEvent("/m/lib/.cache/a.mp4")) == []
    assert events_for(FileCreatedEvent("/m/lib/x/.a.mp4.part")) == []
    # a download finishing: renamed from .part to the real name
    assert events_for(FileMovedEvent("/m/lib/x/a.mp4.part", "/m/lib/x/a.mp4")) == [(7, "/m/lib/x")]


async def test_flush_enqueues_deduped_scans(app, clean):
    w = LibraryWatcher(app.state.pool, quiet_seconds=0)
    w.debouncer.add(1, "/m/lib/a")
    w.debouncer.add(1, "/m/lib/a/b")
    w.debouncer.add(1, "/m/lib/c")
    assert await w.flush() == 2
    w.debouncer.add(1, "/m/lib/a")
    assert await w.flush() == 1
    rows = await app.state.pool.fetch("SELECT payload FROM jobs ORDER BY id")
    assert [r["payload"]["path"] for r in rows] == ["/m/lib/a", "/m/lib/c"]  # third collapsed


async def test_flush_escalates_to_full_scan(app, clean):
    w = LibraryWatcher(app.state.pool, quiet_seconds=0)
    for i in range(MAX_DIRS_PER_LIBRARY + 1):
        w.debouncer.add(1, f"/m/lib/d{i}")
    assert await w.flush() == 1
    assert await app.state.pool.fetchval("SELECT payload FROM jobs") == {"library_id": 1, "path": None}


async def test_flush_root_means_full_scan(app, clean):
    w = LibraryWatcher(app.state.pool, quiet_seconds=0)
    w._roots = {1: "/m/lib"}
    w.debouncer.add(1, "/m/lib")
    w.debouncer.add(1, "/m/lib/sub")
    assert await w.flush() == 1
    assert await app.state.pool.fetchval("SELECT dedupe_key FROM jobs") == "scan:1"


# --- observer lifecycle -----------------------------------------------------------

import asyncio  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402

import pytest  # noqa: E402

from app import watcher as watcher_mod  # noqa: E402


class FakeObserver:
    """Stands in for watchdog's Observer; `behaviour(path)` runs inside start()."""
    behaviour = staticmethod(lambda path: None)
    started: list[str] = []

    def __init__(self):
        self.path = None

    def schedule(self, handler, path, recursive):
        self.path = path

    def start(self):
        FakeObserver.behaviour(self.path)
        FakeObserver.started.append(self.path)

    def stop(self):
        pass

    def join(self, timeout=None):
        pass


@pytest.fixture
def fake_observer(monkeypatch):
    FakeObserver.started = []
    FakeObserver.behaviour = staticmethod(lambda path: None)
    monkeypatch.setattr(watcher_mod, "Observer", FakeObserver)
    return FakeObserver


async def _libs(pool, n):
    paths = [tempfile.mkdtemp(dir=os.environ["MEDIA_DIR"]) for _ in range(n)]
    ids = [await pool.fetchval("INSERT INTO library_paths (path, watch) VALUES ($1, true) RETURNING id", p)
           for p in paths]
    return ids, paths


async def _states(pool):
    return {r["id"]: (r["watch_state"], r["watch_error"])
            for r in await pool.fetch("SELECT id, watch_state, watch_error FROM library_paths")}


async def test_slow_watch_setup_does_not_block_the_event_loop(app, clean, fake_observer):
    """Regression: registering watches on a big/network tree ran on the event loop and
    froze the job loop (queued jobs sat idle with nothing in the logs)."""
    import time
    fake_observer.behaviour = staticmethod(lambda path: time.sleep(0.6))
    await _libs(app.state.pool, 1)
    w = LibraryWatcher(app.state.pool)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(ticker())
    await w.reload()
    t.cancel()
    assert ticks >= 10
    w.stop()


async def test_watch_limit_failure_is_reported_not_fatal(app, clean, fake_observer):
    (bad, good), (bad_path, good_path) = await _libs(app.state.pool, 2)

    def start(path):
        if path == bad_path:
            raise OSError(28, "inotify watch limit reached")

    fake_observer.behaviour = staticmethod(start)
    w = LibraryWatcher(app.state.pool)
    await w.reload()  # must not raise
    states = await _states(app.state.pool)
    assert states[good] == ("active", None)
    assert states[bad][0] == "error" and "max_user_watches" in states[bad][1]
    assert fake_observer.started == [good_path]
    w.stop()


async def test_reload_is_incremental(app, clean, fake_observer):
    (a, b), (pa, pb) = await _libs(app.state.pool, 2)
    w = LibraryWatcher(app.state.pool)
    await w.reload()
    assert sorted(fake_observer.started) == sorted([pa, pb])
    fake_observer.started.clear()

    await app.state.pool.execute("UPDATE library_paths SET watch = false WHERE id = $1", b)
    await w.reload()
    assert fake_observer.started == []  # library a was not re-registered
    states = await _states(app.state.pool)
    assert states == {a: ("active", None), b: (None, None)}

    await app.state.pool.execute("UPDATE library_paths SET watch = true WHERE id = $1", b)
    await w.reload()
    assert fake_observer.started == [pb]
    w.stop()


async def test_unavailable_path_is_reported(app, clean, fake_observer):
    (lid,), (path,) = await _libs(app.state.pool, 1)
    os.rmdir(path)
    w = LibraryWatcher(app.state.pool)
    await w.reload()
    assert (await _states(app.state.pool))[lid] == ("error", "library path is not available")


async def test_failed_watch_releases_its_inotify_descriptor(app, clean, fake_observer):
    """Regression: a start that hit the watch limit part-way leaked the inotify
    descriptor (and every watch on it), starving other libraries of watches."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    opened = []

    def start(path):
        opened.append(libc.inotify_init())  # what watchdog does before adding watches
        raise OSError(28, "inotify watch limit reached")

    fake_observer.behaviour = staticmethod(start)
    await _libs(app.state.pool, 1)
    before = watcher_mod._inotify_fds()
    w = LibraryWatcher(app.state.pool)
    await w.reload()
    assert opened and opened[0] > 0
    assert watcher_mod._inotify_fds() == before  # the orphaned descriptor was closed
