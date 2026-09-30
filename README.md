# goon-streamer

Self-hosted, privacy-first media organizer (Stash-inspired). FastAPI + PostgreSQL + FFmpeg,
React frontend (coming). Everything runs via Docker Compose; no telemetry, no outbound calls.

## Running

```sh
cp .env.example .env          # set POSTGRES_PASSWORD, MEDIA_ROOT
docker compose up -d --build  # dev mode (source mounted, auto-reload)
curl localhost:8420/api/health
docker compose run --rm api pytest
```

Production-style (no dev override): `docker compose -f compose.yaml up -d --build`.

Media is mounted **read-only** at `/media`; generated content lives in `GENERATED_ROOT`
(mounted at `/data/generated`). The database has no published port, and the API
listens only on `127.0.0.1`.

## Services

| service  | role |
|----------|------|
| `db`     | PostgreSQL 17, internal network only |
| `api`    | FastAPI on `127.0.0.1:${API_PORT}` (docs at `/api/docs`) |
| `worker` | job queue runner + filesystem watcher (single instance) |

## Ingestion

- `POST /api/libraries {"path": "/media/...", "watch": true}`: paths must be inside `/media`,
  must not overlap each other or the generated dir.
- `POST /api/libraries/{id}/scan` (optionally `{"path": subdir}`), `POST /api/libraries/scan` for all.
- `GET /api/jobs`, `GET /api/jobs/{id}`, `POST /api/jobs/{id}/cancel`.

Scan behaviour: unchanged files are skipped without DB or probe work; moved files are
matched by content hash (oshash + size) and keep their metadata; vanished files are only
marked missing; a library whose root is unavailable (unmounted drive) fails the scan
instead of marking everything missing; files modified in the last 10s are deferred to a
delayed re-scan. Folders of images become galleries, as do `.zip`/`.cbz` archives.

The watcher uses inotify. For network mounts, or libraries with more directories than
`fs.inotify.max_user_watches`, set `WATCHER_POLLING=true`.

## Design invariants: no feedback loops

Automated components must never be able to trigger each other indefinitely.
Every new automated behaviour must preserve these rules:

1. **Watcher output never re-enters watcher input.** The file watcher observes only
   configured library paths under `/media`, which is mounted read-only. All generated
   artifacts go to `/data/generated`, which is never a library path (enforced at
   library-path validation time: no library may be inside or contain the generated dir).
2. **Events are debounced and deduplicated.** Filesystem events are coalesced over a
   quiet window (with a max wait) and collapsed to topmost directories before a scan
   is enqueued; at most one scan per key is queued, and the same key never runs twice
   concurrently.
3. **Jobs don't spawn unbounded jobs.** A scan may enqueue generation jobs; generation
   jobs never enqueue scans. The only self-re-enqueue is a delayed re-scan while files
   are still being written, which stops once they settle.
4. **Database triggers are non-recursive.** Triggers only update denormalized columns
   on *other* tables that carry no triggers back (e.g. `gallery_images` -> `galleries.image_count`).
5. **Plugins/hooks (later) run with a depth guard.** Actions caused by a hook carry
   the originating event id; a hook is never re-fired by its own side effects, and
   there is a hard cap on hook chain depth.
6. **Dev reloader watches source only** (`--reload-dir app`), never data or caches.
