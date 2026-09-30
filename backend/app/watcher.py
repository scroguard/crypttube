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


class LibraryWatcher:
    def __init__(self, pool: asyncpg.Pool, *, polling: bool = False,
                 quiet_seconds: float = 5, max_wait_seconds: float = 60):
        self.pool = pool
        self.polling = polling
        self.debouncer = Debouncer(quiet_seconds, max_wait_seconds)
        self._observer = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._roots: dict[int, str] = {}

    def _emit(self, library_id: int, directory: str) -> None:
        # Called on watchdog's thread.
        self._loop.call_soon_threadsafe(self.debouncer.add, library_id, directory)

    async def reload(self) -> None:
        """(Re)build watches from the library_paths table."""
        self._loop = asyncio.get_running_loop()
        self.stop()
        rows = await self.pool.fetch(
            "SELECT id, path FROM library_paths WHERE enabled AND watch ORDER BY id"
        )
        self._roots = {r["id"]: r["path"].rstrip("/") for r in rows}
        if not rows:
            return
        observer = PollingObserver(timeout=10) if self.polling else Observer()
        for r in rows:
            if not os.path.isdir(r["path"]):
                log.warning("not watching %s: path unavailable", r["path"])
                continue
            try:
                observer.schedule(_Handler(r["id"], r["path"], self._emit), r["path"], recursive=True)
            except OSError as e:  # typically inotify watch limit
                log.error("cannot watch %s: %s (raise fs.inotify.max_user_watches "
                          "or set WATCHER_POLLING=true)", r["path"], e)
        observer.start()
        self._observer = observer
        log.info("watching %d librar%s", len(rows), "y" if len(rows) == 1 else "ies")

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

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
