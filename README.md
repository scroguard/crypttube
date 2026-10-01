# goon-streamer

Self-hosted, privacy-first media organizer (Stash-inspired). FastAPI + PostgreSQL + FFmpeg
backend, React + Tailwind frontend. Everything runs via Docker Compose; no telemetry, no
outbound calls, no external fonts/CDNs.

## Running

```sh
cp .env.example .env          # set POSTGRES_PASSWORD, MEDIA_ROOT, PUID/PGID
docker compose up -d --build  # production: compose.yaml only
```

Open http://localhost:8420. On first run you're asked to create the admin account; then
add a library under **Admin → Libraries** (paths are inside the container, under `/media`).

Only the `web` service (nginx serving the built app and proxying `/api`) is published;
the API and database are internal. `compose.yaml` is self-contained, so stack managers
(Dockhand, Portainer, …) can deploy it directly from the repo.

**Reaching it from elsewhere / reverse proxies.** Nothing needs editing in `compose.yaml`
(so pulling updates never undoes your setup); use environment variables:

- `WEB_BIND=0.0.0.0` (or a LAN address) publishes the UI beyond localhost, for other
  machines or a reverse proxy on another host. Target: `http://<host>:${WEB_PORT}`.
- For a reverse proxy running in Docker on the same host (Nginx Proxy Manager, Traefik,
  Caddy, cloudflared…), set `PROXY_NETWORK=<the proxy's existing network>` and
  `COMPOSE_FILE=compose.yaml:compose.proxy.yaml`, then point the proxy at
  `http://goonstreamer-web:80`. The stack joins that network; redeploying never
  detaches the proxy.
- With HTTPS at the proxy set `COOKIE_SECURE=true`. Allow large request bodies and long
  timeouts on the proxy for uploads and streaming (e.g. nginx `client_max_body_size 0;`
  — Cloudflare's free plan caps uploads at 100 MB per request).
- `GET /healthz` on the web container answers without the API, for proxy health checks;
  `web` and `api` also have Docker health checks.

**Development** is opt-in via `compose.dev.yaml` (Vite with hot reload, API/worker
auto-reload with source mounted, API also on `127.0.0.1:${API_PORT:-8421}`):

```sh
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
# or put COMPOSE_FILE=compose.yaml:compose.dev.yaml in .env and use plain `docker compose`
```

Tests:

```sh
docker compose run --rm goonstreamer-api pytest                     # backend (134 tests; needs the dev compose)
docker compose run --rm goonstreamer-web npx vitest run             # frontend unit tests (dev compose)
docker compose run --rm goonstreamer-api python -m scripts.bench_browse # list API on 200k scenes
scripts/e2e.sh                                           # Playwright, isolated stack
```

`scripts/e2e.sh` generates fixture media and runs the full UI journey (setup, scan,
playback/seek/transcode, editing, filters, lightbox, restricted user) against the
production build in a separate compose project (`goon-e2e`) with its own database; it
never touches your data. Screenshots land in `frontend/test-results/screens/`.
Behind HTTPS (reverse proxy with TLS), set `COOKIE_SECURE=true`.

Media is mounted **read-only** at `/media`. Everything the app writes lives in three
Docker volumes: `pgdata` (database), `uploads` (users' own libraries) and `generated`
(thumbnails, sprites, performer/studio/tag images, at `/data/generated`). Back up all
three; `generated` is rebuildable from the media except uploaded entity images. The database has no published port, and the API
listens only on `127.0.0.1`.

## Services

Service and container names share the `goonstreamer-` prefix so they can't collide with
other stacks on the same Docker host.

| service / container    | role |
|------------------------|------|
| `goonstreamer-web`     | nginx: React app + `/api` proxy on `127.0.0.1:${WEB_PORT:-8420}` (dev: Vite) |
| `goonstreamer-api`     | FastAPI (docs at `/api/docs`), internal |
| `goonstreamer-db`      | PostgreSQL 17, internal |
| `goonstreamer-worker`  | job queue runner + filesystem watcher (single instance) |

## Frontend

React 19 SPA (`frontend/`), TanStack Query for caching, react-virtuoso for virtualized
window-scrolled grids (only on-screen cards exist in the DOM; Back restores the exact
scroll position), filters kept in the URL. Highlights:

- **Player**: picks direct play / remux / transcode by asking the browser what it can
  decode, falls back automatically on error, manual quality menu (Original, 1080p…360p),
  seek-bar sprite previews, keyboard shortcuts (space/k, j/l, ←/→, ↑/↓, m, f), per-user
  resume, play counts.
- **Grouped by performer**: Scenes and Images open grouped (a section per performer with
  their first dozen items and a link to all of them, then "Unassigned"); a toggle switches
  to the flat grid, remembered per browser. `GET /api/groups/{scenes,images}` backs it:
  same filters/sort/ACL as the lists, performers paged by name, strategy chosen by how
  many items match (~55 ms per page at 200k scenes).
- **Paging**: lists scroll continuously by default; an *items per page* selector
  (10/20/30/50/100/200, remembered per browser) switches every list to pages with
  Previous/Next.
- **Grids**: hover-scrub previews on scene cards, bulk select (tags, performers, rating,
  organized), lazy-loaded WebP thumbnails.
- **Lightbox**: keyboard/swipe navigation, neighbour preloading, click-to-zoom, loads
  further pages as you approach the end.
- **Assigning performers/tags**: directly on the scene page (inline pickers), in the image
  lightbox (Edit), or in bulk on any scene or image grid (Select → add/remove performers
  and tags). **Select all matching** selects everything that matches the current search and
  filters, loaded or not (`GET /api/ids/{scenes,images}`, editable items only, up to
  20,000), so "search a performer's folder → select all → add performer" is three steps.
- **Editing** (editors/admins): scenes, galleries, images, performers (incl. images),
  studios, tags (hierarchy, merge); create tags/performers/studios inline from pickers.
- **Admin**: libraries (watch, exclusions, scan), live job monitor, users and per-user
  content restrictions.

nginx sends a strict CSP (`default-src 'self'`), `no-referrer`, `nosniff`, frame denial,
and keeps access logs off (URLs identify content).

## Ingestion

- `POST /api/libraries {"path": "/media/...", "watch": true}`: paths must be inside `/media`,
  must not overlap each other or the generated dir.
- `POST /api/libraries/{id}/scan` (optionally `{"path": subdir}` to rescan just one folder,
  e.g. a single artist), `POST /api/libraries/scan` for all.
- `GET /api/libraries/browse?path=…`: subfolders of a folder under `/media` (admin; never
  leaves the media root; skips hidden and symlinked folders like the scanner does). The UI's
  **Browse…** and **Scan folder…** pickers use it.
- `GET /api/jobs`, `GET /api/jobs/{id}`, `POST /api/jobs/{id}/cancel`.

Scan behaviour: unchanged files are skipped without DB or probe work; moved files are
matched by content hash (oshash + size) and keep their metadata; vanished files are only
marked missing; a library whose root is unavailable (unmounted drive) fails the scan
instead of marking everything missing; files modified in the last 10s are deferred to a
delayed re-scan. Folders of images become galleries, as do `.zip`/`.cbz` archives.

The watcher uses inotify, which needs one watch per folder. Registering them happens in
the background (jobs keep running) and can take a while on big or network-mounted
libraries; each library card in **Admin → Libraries** shows *Starting watcher…*,
*Watching*, or *Watch failed* with the reason. If a library has more folders than
`fs.inotify.max_user_watches` (a per-host-user limit), raise it on the host, e.g.
`sysctl -w fs.inotify.max_user_watches=1048576` (persist in `/etc/sysctl.d/`), or set
`WATCHER_POLLING=true`. Note that inotify on NFS/SMB mounts only sees changes made through
this host; for files added from other machines, use **Scan** / **Scan folder…**.

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
| `POST /api/generate` `{force?, library_id?}` | queue screenshots/sprites that are missing or unusable; `force` discards and rebuilds everything in scope (admin) |
| `POST /api/scenes/{id}/regenerate` | rebuild one scene's assets (whoever may edit it) |
| `POST /api/my/libraries/{id}/regenerate` | rebuild one of your own libraries |

Thumbnail widths are fixed (160/320/640/1280) so the cache can't be blown up by
arbitrary sizes. Generated assets are keyed by content version (scene oshash, image
file id + mtime); requests that carry the current `v=` get
`Cache-Control: private, max-age=31536000, immutable`, others revalidate. `private`
keeps shared proxies from caching anything.

Scans queue asset generation for new/changed videos in the worker.

**Repairing generated content.** *Generate missing previews* (Admin → Libraries) creates
whatever is missing, and treats empty or unreadable files as missing. *Rebuild previews*
(per library, per scene, per user library) or *Rebuild all previews…* discards and
regenerates screenshots, scrub sprites and cached thumbnails. Each rebuilt item's asset
revision is bumped and is part of its image URLs (`v=<hash>.<rev>`), so browsers fetch the
new files instead of their year-long-cached copies. Failures are listed with reasons in
the job (Admin → Jobs); if the `generated` volume isn't writable the job fails saying so.
Uploaded performer/studio/tag images are never touched.

## Browse API

`GET /api/{scenes,galleries,images,performers,studios,tags}` and `/{id}` for details;
`GET /api/galleries/{id}/images` in gallery order. Lists take `limit` (≤200), `sort`,
`desc`, `cursor` (from `next_cursor`), `with_total`, `thumb_w`, plus filters (`q`: title
**or file path** contains, so a folder name finds everything in it;
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
  Other sources (UI: **Add/Change image**): `POST …/image/from-library {image_id | scene_id}`
  (anything the user can see), `POST …/image/from-path {path}` (admins: any image file in
  the media store, browsable with previews), `POST …/image/from-url {url}`.
- **From URL is the app's only outbound request** and happens only on that explicit action.
  `app/fetch.py` allows http(s) to public internet addresses only (no loopback, LAN,
  link-local/metadata, CGNAT…), connects to the address it validated, re-validates every
  redirect, caps size and time, and sends no cookies or referrer. `URL_IMAGE_FETCH=false`
  disables it.
- `POST /api/scenes/{id}/play`, `PUT /api/scenes/{id}/resume {"time": s}`.

Writes queue a debounced `refresh_counts` job; list counts catch up within seconds.

## Authentication & roles

- Server-side sessions in an HttpOnly, SameSite=Lax cookie (needed so `<video>`/`<img>`
  requests are authenticated). Only SHA-256(token) is stored; 30-day sliding expiry;
  password change or deactivation revokes sessions immediately. No IPs are stored.
- Passwords: argon2id. Login throttled per (IP, username) with exponential backoff;
  unknown usernames take the same time as wrong passwords.
- CSRF: state-changing requests must send an `X-Requested-With` header.
- Roles: `viewer` (browse/stream, own resume positions), `editor` (+ metadata edits),
  `admin` (+ users, libraries, jobs; never restricted). At least one active admin
  always remains.
- Every router is mounted with its minimum role; a test asserts every route except
  health/status/setup/login returns 401 without a session.
- Responses carry `nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`,
  and `Cache-Control: no-store` unless a media route sets its own caching.

Admin API: `GET/POST /api/users`, `GET/PATCH/DELETE /api/users/{id}`,
`PUT /api/users/{id}/restrictions {"tag_ids", "studio_ids", "performer_ids"}`.
Self: `/api/auth/{status,login,logout,me,password,sessions}`.

## User libraries

Besides the global libraries an admin adds from `/media`, users can have their own:

- An admin enables uploads per user (**Admin → Users**) and may set a storage quota.
- The user creates libraries under **My uploads** and uploads videos, images, zip
  archives or whole folders from the browser (streamed to disk, any size, with progress;
  the quota is enforced while streaming). Files land in the `uploads` volume at
  `/uploads/<user id>/<random>/…` and go through the normal scan → probe → generate
  pipeline; generated thumbnails live in the shared `generated` volume and are only ever
  served through the API's visibility checks.
- Each library is **private**, **shared with selected users**, or **shared with everyone**;
  only the owner can change that, at any time.
- **Private means private from admins too**: an admin sees that a library exists, who owns
  it and how much space it uses (and can delete it), but can't browse, search or play its
  content, and scan-job paths inside user libraries are redacted. (Someone with shell
  access to the Docker host can of course read the volume.)
- Only the owner can edit or delete what's in their library, whatever their role.
  People it is shared with can browse and play. An admin's deny rules for a user still
  apply to content shared with that user.
- **Personal metadata**: tags, performers and studios created by a regular user belong
  to that user. Others see them only when attached to content they can see; names are
  unique per owner, so they neither collide nor leak through "name taken" errors.
  Editors/admins create the global ones, which anyone may use on their own content.
- Deleting a user (or a library) removes the uploaded files as well.

API: `/api/my/libraries` (list/create), `/api/my/libraries/{id}` (PATCH/DELETE),
`/api/my/libraries/{id}/files` (GET list, `PUT ?path=` upload raw body, `DELETE ?path=`),
`/api/my/share-targets`; admin overview at `/api/libraries/user`.

## Access control

Three layers, all applied inside SQL by one clause builder (`app/acl.py`): library
visibility (above), personal metadata (above), and per-user deny rules.
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
