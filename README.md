# goon-streamer

Self-hosted, privacy-first media organizer (Stash-inspired). FastAPI + PostgreSQL + FFmpeg,
React frontend (coming). Everything runs via Docker Compose; no telemetry, no outbound calls.

## Running

```sh
cp .env.example .env          # set POSTGRES_PASSWORD, MEDIA_ROOT, PUID/PGID (`id -u`/`id -g`)
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

## Media delivery

| endpoint | notes |
|----------|-------|
| `GET/HEAD /api/scenes/{id}/stream` | original file, HTTP Range / 206, `If-Range` |
| `GET /api/scenes/{id}/stream.mp4?mode=remux\|transcode&start=&height=` | live fragmented MP4 via ffmpeg; seek with `start`; max `MAX_TRANSCODES` concurrent (503 beyond); ffmpeg is killed on disconnect |
| `GET /api/scenes/{id}/playback` | preferred modes (`direct` → `remux` → `transcode`), URLs, sprite VTT |
| `GET /api/scenes/{id}/screenshot?w=&v=` | WebP, generated on demand if missing |
| `GET /api/scenes/{id}/sprite.vtt`, `sprite.webp` | scrub previews (9×9 keyframe grid + WebVTT `#xywh`) |
| `GET /api/images/{id}/thumbnail?w=&v=` | WebP, on demand, EXIF-rotated, never upscaled; works for zip members |
| `GET /api/images/{id}/file` | original image |
| `POST /api/generate` | queue screenshots/sprites for all scenes lacking them |

Thumbnail widths are fixed (160/320/640/1280) so the cache can't be blown up by
arbitrary sizes. Generated assets are keyed by content version (scene oshash, image
file id + mtime); requests that carry the current `v=` get
`Cache-Control: private, max-age=31536000, immutable`, others revalidate. `private`
keeps shared proxies from caching anything.

Scans queue asset generation for new/changed videos in the worker.

## Browse API

`GET /api/{scenes,galleries,images,performers,studios,tags}` and `/{id}` for details;
`GET /api/galleries/{id}/images` in gallery order. Lists take `limit` (≤200), `sort`,
`desc`, `cursor` (from `next_cursor`), `with_total`, `thumb_w`, plus filters (`q`,
`tag_ids` + `tag_mode=all|any` + `include_sub_tags`, `exclude_tag_ids`, `performer_ids`,
`studio_ids` + `include_sub_studios`, `rating_min`, `organized`, scene-only
`duration_min/max`, `height_min`, `date_from/to`, `played`, `file_status`).
Unknown parameters are rejected (422).

How it stays fast (`python -m scripts.bench_browse` measures it on 200k scenes):

- **Keyset pagination** with an index on exactly `(sort expression, id)` for every
  sort; page 1,000 costs the same as page 1. A test fails if a sort loses its index.
- **One round-trip per list**: the page is selected first, then studio/performers/tags
  are aggregated with `jsonb_agg` for just those rows via LATERAL joins.
- **Denormalized `tag_ids`/`performer_ids` arrays** (trigger-maintained from the link
  tables) make ACL checks and tag filters per-row operations.
- **Tag/studio trees are expanded before the query** and plans are always made for the
  actual parameter values, so the planner knows how selective a filter is.
- **Entity counts are cached** (`entity_counts`, per viewer) and refreshed by a debounced
  worker job after scans/edits, instead of counting thousands of rows per page.

## Edit API

- `PATCH /api/{scenes,galleries,images}/{id}`: partial update; `tag_ids`/`performer_ids` replace the set.
- `POST /api/{scenes,galleries,images}/bulk`: `{"ids": [...], "set": {studio_id, rating, organized},
  "tags": {"mode": "add|remove|set", "ids": [...]}, "performers": {...}}`, set-based SQL.
- `POST/PATCH/DELETE /api/{performers,studios,tags}`: hierarchy cycles return 409;
  `POST /api/tags/{id}/merge {"source_ids": [...]}` folds tags together.
- `PUT/GET/DELETE /api/{performers,studios,tags}/{id}/image`: raw image body, stored as WebP (≤1024px).
- `POST /api/scenes/{id}/play`, `PUT /api/scenes/{id}/resume {"time": s}`.

Writes queue a debounced `refresh_counts` job; list counts catch up within seconds.

## Access control

Per-user deny rules (`user_hidden_tags/studios/performers`) are expanded once per
request (sub-tags, child studios, performers carrying a hidden tag) and enforced inside
every list/detail query; hidden items 404 and don't appear in counts.

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
   jobs never enqueue scans, and `refresh_counts` enqueues nothing. The only
   self-re-enqueue is a delayed re-scan while files are still being written, which
   stops once they settle.
4. **Database triggers are non-recursive.** Triggers only update denormalized columns
   on *other* tables that carry no triggers back (e.g. `gallery_images` -> `galleries.image_count`).
5. **Plugins/hooks (later) run with a depth guard.** Actions caused by a hook carry
   the originating event id; a hook is never re-fired by its own side effects, and
   there is a hard cap on hook chain depth.
6. **Dev reloader watches source only** (`--reload-dir app`), never data or caches.
