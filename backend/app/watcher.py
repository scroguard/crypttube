"""Filesystem watcher: turns bursts of filesystem events into a few debounced scan jobs.

Events arrive on watchdog's thread and are handed to the asyncio loop. Nothing is
scanned until the library has been quiet for `quiet_seconds` (or `max_wait_seconds`
has passed since the first pending event, so constant activity can't starve scans).
Affected directories are collapsed to their topmost ancestors before enqueueing.
"""

import asyncio
import logging
import os
import time
from collections.abc import Callable

import asyncpg
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app import jobs, media

log = logging.getLogger(__name__)

# Beyond this many distinct directories, one full-library scan is cheaper.
MAX_DIRS_PER_LIBRARY = 50


def collapse(dirs: set[str]) -> list[str]:
    """Drop directories whose ancestor is also in the set."""
    kept: list[str] = []
    for d in sorted(dirs):
        if not any(d == k or d.startswith(k.rstrip("/") + "/") for k in kept):
            kept.append(d)
    return kept


def scan_dedupe_key(library_id: int | None, path: str | None = None) -> str:
    if library_id is None:
        return "scan:all"
    return f"scan:{library_id}" + (f":{path}" if path else "")


class Debouncer:
    def __init__(self, quiet_seconds: float = 5, max_wait_seconds: float = 60,
                 clock: Callable[[], float] = time.monotonic):
        self.quiet = quiet_seconds
        self.max_wait = max_wait_seconds
        self.clock = clock
        self.pending: dict[int, set[str]] = {}
        self._first = 0.0
        self._last = 0.0

    def add(self, library_id: int, directory: str) -> None:
        now = self.clock()
        if not self.pending:
            self._first = now
        self._last = now
        self.pending.setdefault(library_id, set()).add(directory)

    def pop_ready(self) -> dict[int, list[str]]:
        if not self.pending:
            return {}
        now = self.clock()
        if now - self._last < self.quiet and now - self._first < self.max_wait:
            return {}
        ready = {lib: collapse(dirs) for lib, dirs in self.pending.items()}
        self.pending = {}
        return ready


class _Handler(FileSystemEventHandler):
    def __init__(self, library_id: int, root: str, emit: Callable[[int, str], None]):
        self.library_id = library_id
        self.root = root.rstrip("/")
        self.emit = emit

    def _inside(self, path: str) -> bool:
        return path == self.root or path.startswith(self.root + "/")

    def _relevant(self, path: str, is_dir: bool) -> bool:
        if any(part.startswith(".") for part in path[len(self.root):].split("/") if part):
            return False
        return is_dir or media.classify(path) is not None

    def _dir_for(self, path: str, is_dir: bool, removed: bool) -> str:
        # A created directory is scanned itself; for files and removed
        # directories the parent is what changed.
        d = path if is_dir and not removed else os.path.dirname(path)
        return d if self._inside(d) else self.root

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in ("opened", "closed_no_write"):
            return
        # A directory's own "modified" event only means its entries changed; those
        # entries report their own create/delete/move events, which are narrower.
        if event.is_directory and event.event_type == "modified":
            return
        src = os.fsdecode(event.src_path)
        if event.event_type == "moved":
            dest = os.fsdecode(event.dest_path)
            if self._inside(src) and self._relevant(src, event.is_directory):
                self.emit(self.library_id, self._dir_for(src, event.is_directory, removed=True))
            if self._inside(dest) and self._relevant(dest, event.is_directory):
                self.emit(self.library_id, self._dir_for(dest, event.is_directory, removed=False))
            return
        if self._relevant(src, event.is_directory):
            removed = event.event_type == "deleted"
            self.emit(self.library_id, self._dir_for(src, event.is_directory, removed))


WATCH_LIMIT_HINT = ("too many folders for the kernel's inotify watch limit; raise "
                    "fs.inotify.max_user_watches on the host or set WATCHER_POLLING=true")


def _inotify_fds() -> set[int]:
    """This process's open inotify descriptors (Linux; empty elsewhere)."""
    fds = set()
    try:
        for name in os.listdir("/proc/self/fd"):
            try:
                if os.readlink(f"/proc/self/fd/{name}") == "anon_inode:inotify":
                    fds.add(int(name))
            except OSError:
                continue
    except OSError:
        pass
    return fds


def _stop_observer(observer) -> None:
    try:
        observer.stop()
        observer.join(timeout=5)
    except Exception:  # never started, or already dead
        pass


class LibraryWatcher:
    """One watchdog observer per watched library.

    Starting an inotify observer registers a watch on every folder of the library,
    synchronously. That can take minutes on a big tree or a network mount, so it runs
    in a worker thread: the job loop keeps running meanwhile. Reloads are incremental
    (only libraries that changed are restarted), and a library that can't be watched
    is reported in library_paths.watch_state/watch_error instead of taking the worker down.
    """

    def __init__(self, pool: asyncpg.Pool, *, polling: bool = False,
                 quiet_seconds: float = 5, max_wait_seconds: float = 60):
        self.pool = pool
        self.polling = polling
        self.debouncer = Debouncer(quiet_seconds, max_wait_seconds)
        self._observers: dict[int, tuple[str, object]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._roots: dict[int, str] = {}
        self._lock = asyncio.Lock()

    def _emit(self, library_id: int, directory: str) -> None:
        # Called on watchdog's thread.
        self._loop.call_soon_threadsafe(self.debouncer.add, library_id, directory)

    def _start(self, library_id: int, path: str) -> tuple[object | None, str | None]:
        """Runs in a thread. Returns (observer, None) or (None, error message)."""
        if not os.path.isdir(path):
            return None, "library path is not available"
        observer = PollingObserver(timeout=10) if self.polling else Observer()
        before = _inotify_fds()
        try:
            observer.schedule(_Handler(library_id, path, self._emit), path, recursive=True)
            observer.start()
        except OSError as e:
            _stop_observer(observer)
            # watchdog opens an inotify descriptor and adds watches folder by folder; when
            # that fails part-way it never closes the descriptor, so the watches already
            # added stay held. The limit is per host user, so the leak would break watching
            # for every other library (and container) running as the same user.
            # Closing the orphaned descriptor releases all of its watches.
            for fd in _inotify_fds() - before:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return None, WATCH_LIMIT_HINT if e.errno == 28 else str(e)
        return observer, None

    async def reload(self) -> None:
        """Bring running observers in line with library_paths (enabled AND watch)."""
        self._loop = asyncio.get_running_loop()
        async with self._lock:
            rows = await self.pool.fetch("SELECT id, path FROM library_paths WHERE enabled AND watch ORDER BY id")
            wanted = {r["id"]: r["path"].rstrip("/") for r in rows}
            self._roots = wanted

            for lid in [lid for lid, (path, _) in self._observers.items() if wanted.get(lid) != path]:
                _, observer = self._observers.pop(lid)
                await asyncio.to_thread(_stop_observer, observer)
            await self.pool.execute(
                "UPDATE library_paths SET watch_state = NULL, watch_error = NULL "
                "WHERE NOT (enabled AND watch) AND watch_state IS NOT NULL")

            new = {lid: path for lid, path in wanted.items() if lid not in self._observers}
            if new:
                await self.pool.execute(
                    "UPDATE library_paths SET watch_state = 'starting', watch_error = NULL WHERE id = ANY($1)",
                    list(new))
            for lid, path in new.items():
                observer, error = await asyncio.to_thread(self._start, lid, path)
                if observer is not None:
                    self._observers[lid] = (path, observer)
                else:
                    log.error("cannot watch %s: %s", path, error)
                await self.pool.execute(
                    "UPDATE library_paths SET watch_state = $2, watch_error = $3 WHERE id = $1",
                    lid, "active" if observer is not None else "error", error)
            log.info("watching %d of %d librar%s", len(self._observers), len(wanted),
                     "y" if len(wanted) == 1 else "ies")

    def stop(self) -> None:
        for _, observer in self._observers.values():
            _stop_observer(observer)
        self._observers.clear()

    async def flush(self) -> int:
        """Enqueue scans for directories that have gone quiet. Returns jobs enqueued."""
        count = 0
        for library_id, dirs in self.debouncer.pop_ready().items():
            # The library root itself means a full scan (and shares its dedupe key).
            if len(dirs) > MAX_DIRS_PER_LIBRARY or self._roots.get(library_id) in dirs:
                dirs = [None]
            for d in dirs:
                await jobs.enqueue(
                    self.pool, "scan", {"library_id": library_id, "path": d},
                    dedupe_key=scan_dedupe_key(library_id, d),
                )
                count += 1
        return count

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.flush()
            except Exception:
                log.exception("watcher flush failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                pass
        self.stop()
