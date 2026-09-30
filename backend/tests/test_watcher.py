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
