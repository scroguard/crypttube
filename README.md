# CryptTube

Self-hosted, privacy-first media organizer (Stash-inspired). FastAPI + PostgreSQL + FFmpeg
backend, React + Tailwind frontend. Everything runs via Docker Compose; no telemetry, no
external fonts/CDNs, and no outbound calls except "set a picture from a URL", when someone
asks for it (`URL_IMAGE_FETCH=false` turns it off).

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
  `http://crypttube-web:80`. The stack joins that network; redeploying never
  detaches the proxy.
- Set `TRUSTED_PROXY` to the reverse proxy's own address (e.g. `172.18.0.5`), so failed
  logins are counted per visitor. Without it every visitor looks like the proxy, and one
  person's wrong passwords slow down everyone's logins for that username. Whatever it
  trusts may claim to be any visitor, so don't give a whole network (CIDR) unless only
  the proxy is on it: other containers on a shared proxy network could then dodge the
  login throttle. In Docker, give the proxy a fixed address so it can't change.
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
docker compose run --rm crypttube-api pytest                     # backend (134 tests; needs the dev compose)
docker compose run --rm crypttube-web npx vitest run             # frontend unit tests (dev compose)
docker compose run --rm crypttube-api python -m scripts.bench_browse # list API on 200k scenes
scripts/e2e.sh                                           # Playwright, isolated stack
```

`scripts/e2e.sh` generates fixture media and runs the full UI journey (setup, scan,
playback/seek/transcode, editing, filters, lightbox, restricted user) against the
production build in a separate compose project (`crypttube-e2e`) with its own database; it
never touches your data. Screenshots land in `frontend/test-results/screens/`.
Behind HTTPS (reverse proxy with TLS), set `COOKIE_SECURE=true`.

Media is mounted at `/media`. The app never changes or moves a file there; it deletes one
only when an admin asks (Delete buttons, duplicate removal), which needs the mount to be
writable (`MEDIA_MODE=rw`, the default) and `PUID` to have permission. `MEDIA_MODE=ro`
makes deletion impossible. Everything the app writes lives in three
Docker volumes: `pgdata` (database), `uploads` (users' own libraries) and `generated`
(thumbnails, sprites, performer/studio/tag images, at `/data/generated`). Back up all
three; `generated` is rebuildable from the media except uploaded entity images. Neither the database nor
the API has a published port; only the other containers on the stack's network reach them.

The volumes are named `goon-streamer_pgdata`, `goon-streamer_uploads` and
`goon-streamer_generated` (the project's former name), so installs from before the rename
keep their data. If yours carry another prefix (`docker volume ls`), set
`VOLUME_PREFIX` to it. If a redeploy then fails because the volumes "already exist" (they
still carry the old project's label, which some Compose versions won't adopt), set
`EXISTING_VOLUMES=true`: the stack then uses them as they are. Leave it unset on a fresh
install, where the volumes don't exist yet.

## Services

Service and container names share the `crypttube-` prefix so they can't collide with
other stacks on the same Docker host.

| service / container    | role |
|------------------------|------|
| `crypttube-web`     | nginx: React app + `/api` proxy on `127.0.0.1:${WEB_PORT:-8420}` (dev: Vite) |
| `crypttube-api`     | FastAPI (docs at `/api/docs`), internal |
| `crypttube-db`      | PostgreSQL 17, internal |
| `crypttube-worker`  | job queue runner + filesystem watcher (single instance) |

## Frontend

React 19 SPA (`frontend/`), TanStack Query for caching, react-virtuoso for virtualized
window-scrolled grids (only on-screen cards exist in the DOM; Back restores the exact
scroll position), filters kept in the URL.

**Theme.** Colors are named by role, not hue (`bg`, `surface`, `text-muted`, `link`,
`danger-solid`, …): 30 `--color-*` variables in `frontend/src/index.css`, used as Tailwind
classes such as `bg-surface` or `text-link`. Tailwind's own palette is switched off, so a
component can't use a raw color. The default values are the brand's "Crypt" theme. The
fonts (Chakra Petch for headings and small labels, Inter for text, JetBrains Mono for
paths, durations and sizes) are bundled with the app, never loaded from a font service;
their licences are served at `/fonts/`. The logo is drawn inline (`components/Logo.tsx`), so
it follows the theme. Precedence, lowest first: built-in colors, the admin's saved theme,
your own CSS.

**Admin → Theme** sets one theme for everyone: start from a preset (Crypt, the default;
Crypt Lime; Ossuary, true black for OLED screens) and adjust any role with a color picker,
previewed live across the app. Pairs that become hard to read are flagged while you edit,
following the brand's rules: the text roles, link and each label on its button or status
background at 4.5:1; outlines, accent, focus and the delete-button edge at 3:1 against the
backgrounds they sit on. They are warnings, not blocks. Saving stores the 30 colors on the server, served to every page
as `/api/theme.css` (sign-in included); *Use the built-in theme* removes them. Fonts and
corner radii are part of the brand and not editable.

**Your own CSS.** Add `compose.custom-css.yaml` to `COMPOSE_FILE` (e.g.
`COMPOSE_FILE=compose.yaml:compose.custom-css.yaml`, together with any other overrides),
set `CUSTOM_CSS_FILE` to a `.css` file on the host (readable by everyone) and redeploy;
it is served as `/custom.css` and loaded after the app's styles, so it wins. Without it,
an empty `/custom.css` from the image is served. It is served to anyone who can reach the site, signed in or not, so point it
only at a stylesheet. Changing a role restyles everything that uses it:

```css
:root {
  --color-accent: #a3e635;      /* icons, progress, active markers */
  --color-surface: #141414;     /* cards, inputs, menus */
}
```

Any other CSS works too, but class names and markup may change between versions; the
`--color-*` roles are the stable part. The file is re-read on every page load. If an edit
doesn't show, your editor probably replaced the file rather than rewriting it; redeploy
with "force recreate" so the container picks up the new file.

Highlights:

- **Player**: picks direct play / remux / transcode by asking the browser what it can
  decode, falls back automatically on error, manual quality menu (Original, 1080p…360p),
  seek-bar sprite previews, keyboard shortcuts (space/k, j/l, ←/→, ↑/↓, m, f), per-user
  resume, play counts.
- **Home page per user**: an ordered list of sections each user picks, sizes, renames and
  reorders under **Customize** (continue watching, recently added scenes/galleries/images,
  top rated, favourite performers, recently added performers, studios, my uploads). The
  layout is stored per account (`GET/PUT/DELETE /api/me/home`); section content comes from
  the normal list endpoints, so restrictions and ownership apply. Empty sections are hidden.
- **Favourites are personal**: the heart on a performer marks *your* favourite
  (`PUT /api/performers/{id}/favorite`), for any performer you can see.
- **Grouped by performer**: Scenes and Images open grouped (a section per performer with
  their first dozen items and a link to all of them, then "Unassigned"); a toggle switches
  to the flat grid, remembered per browser. `GET /api/groups/{scenes,images}` backs it:
  same filters/sort/ACL as the lists, performers paged by name, strategy chosen by how
  many items match (~55 ms per page at 200k scenes).
- **Paging**: lists scroll continuously by default; an *items per page* selector
  (10/20/30/50/100/200/500/1000, remembered per browser) switches every list to pages with
  Previous/Next.
- **Grids**: hover-scrub previews on scene cards, bulk select (tags, performers, rating,
  organized), lazy-loaded WebP thumbnails.
- **Lightbox**: keyboard/swipe navigation, neighbour preloading, click-to-zoom, loads
  further pages as you approach the end.
- **Assigning performers/tags**: directly on the scene page (inline pickers), in the image
  lightbox (Edit), or in bulk on any scene or image grid (Select → add/remove performers
  and tags). **Select all matching** selects everything that matches the current search and
  filters, loaded or not (`GET /api/ids/{scenes,images}`, editable items only, up to
  100,000), so "search a performer's folder → select all → add performer" is three steps.
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
| `GET /api/images/{id}/thumbnail?w=&v=` | WebP, EXIF-rotated, never upscaled; works for zip members. The sizes the grids use are made by the generate job after each scan; others on first request |
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

### Hardware transcoding

Live transcodes (the `transcode` mode above) can be encoded on a GPU instead of the CPU.
Direct play and remux never encode, so they're unaffected; Live Photo clips and previews
stay on the CPU. Decoding and scaling also stay on the CPU, so every source format works.
Nothing in `compose.yaml` needs editing: add one override file through `COMPOSE_FILE` (together
with any others, e.g. `compose.yaml:compose.proxy.yaml:compose.nvidia.yaml`) and redeploy.

- **AMD or Intel** (VAAPI; Intel 5th-gen Core / Broadwell or newer, and Arc):
  1. On the host, find the render group's id: `getent group render | cut -d: -f3`.
  2. Set `COMPOSE_FILE=compose.yaml:compose.vaapi.yaml` and `RENDER_GID=<that id>`.
  3. With more than one GPU (e.g. an Intel iGPU plus a card), pick the card's render node:
     `ls -l /dev/dri/by-path` shows which `renderD12x` belongs to which PCI device; set
     `HW_DEVICE=/dev/dri/renderD129` (default `renderD128`).
- **NVIDIA** (NVENC): install the NVIDIA driver and the
  [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
  on the host (`docker run --rm --gpus all ubuntu nvidia-smi` should list the card), then set
  `COMPOSE_FILE=compose.yaml:compose.nvidia.yaml`.

At startup the API runs a short test encode. Its log then says either
`hardware encoding on: vaapi` / `nvenc`, or `hardware encoding (...) unavailable,
transcoding on the CPU: <ffmpeg's reason>`, so a GPU that can't encode never breaks
playback. That needs Docker to hand the device over, though: if the GPU itself is missing
(no `/dev/dri`, or no NVIDIA card, driver or toolkit), Docker refuses to start the API.
Take the override out of `COMPOSE_FILE` and redeploy to get it back. A video the GPU encoder can't handle (e.g. larger than it supports at
"Original" quality) is retried on the CPU. With a GPU you can usually raise
`MAX_TRANSCODES` (default 2).

## Browse API

`GET /api/{scenes,galleries,images,performers,studios,tags}` and `/{id}` for details;
`GET /api/galleries/{id}/images` in gallery order. Lists take `limit` (≤1000), `sort`,
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
  access to the Docker host can read the volume, unless uploads are encrypted and the
  key is kept elsewhere: see "Encrypting user uploads".)
- Only the owner can edit or delete what's in their library, whatever their role.
  People it is shared with can browse and play. An admin's deny rules for a user still
  apply to content shared with that user.
- **Personal metadata**: tags, performers and studios created by a regular user belong
  to that user. Others see them only when attached to content they can see; names are
  unique per owner, so they neither collide nor leak through "name taken" errors.
  Editors/admins create the global ones, which anyone may use on their own content.
- Deleting a user (or a library) removes the uploaded files as well.
- Uploads (and their previews) can be stored encrypted: see "Encrypting user uploads".

API: `/api/my/libraries` (list/create), `/api/my/libraries/{id}` (PATCH/DELETE),
`/api/my/libraries/{id}/files` (GET list, `PUT ?path=` upload raw body, `DELETE ?path=`),
`/api/my/share-targets`; admin overview at `/api/libraries/user`.

## Moving files

Select scenes or images and use **Move…** in the selection bar to put them in another
folder, optionally a new one ("a/b" creates both). The file is renamed on disk (copied
across filesystems if needed) and keeps its entry: ratings, tags, performers, play
history and previews stay. Folder galleries follow, and a Live Photo's video moves with
its photo. When the name is taken in the destination, the dialog lets you choose a new
name, adds a number, or leaves the file, as you choose. Admins move within and between
the main libraries; users within and between their own libraries; never between the two.
Files inside zip archives can't be moved individually.

## Live Photos

A photo and a short video (up to 6 s) with the same name in the same folder, like an
iPhone's `IMG_1234.HEIC` + `IMG_1234.MOV`, are paired by the scanner into one Live
Photo. The image gets a LIVE badge; in the image viewer the LIVE button (or a long press
on the picture) plays the motion over it. The video is converted once to a small
H.264/AAC MP4 so it plays instantly in any browser (originals are often HEVC with
uncompressed audio), and it is left out of scene lists and counts. Deleting the photo
deletes its video too. Existing libraries are paired on their next scan.

## Encrypting user uploads

Files users upload to their own libraries, and everything generated from them
(thumbnails, screenshots, scrub previews, viewer copies), can be stored encrypted. The
main libraries are not touched: encrypting the disk they live on is the admin's job.

**What it protects against:** someone who gets the `uploads` and `generated` volumes, a
backup of them, or a disk from the server, but not the key, sees only encrypted data.
It does not protect against someone with root access to the running server (they can
read the key). Keep the key file away from the volumes and their backups, ideally on
another disk.

**How it works:** AES-256-GCM in 1 MiB chunks, a random key per file wrapped with your
master key; each chunk is authenticated, so damage or tampering is detected. Files are
decrypted on the fly when served (seeking and HTTP Range keep working); ffmpeg reads
encrypted videos through a loopback-only stream inside the container, so plaintext never
touches the disk. File names, folder names and titles are not encrypted (search needs
them).

**Turning it on** (any time; existing uploads are encrypted automatically):

1. Create a key file on the Docker host, readable by the user the app runs as (`PUID`):

   ```sh
   openssl rand -base64 32 > /root/crypttube-uploads.key
   chown 1000:1000 /root/crypttube-uploads.key   # your PUID:PGID
   chmod 400 /root/crypttube-uploads.key
   ```

   (Docker hands the containers that one file, so the folder it's in can stay closed to
   everyone else. Only the file itself must be readable by `PUID`.)

2. **Back the key up** somewhere safe and separate (a password manager is fine: it's one
   line of text). **If the key is lost, encrypted uploads can never be read again.**

3. Set `UPLOADS_KEY_FILE=/root/crypttube-uploads.key` in the stack's environment and
   redeploy **recreating the containers** (`docker compose up -d --force-recreate`, or a
   forced redeploy in a stack manager): Compose doesn't notice a new secret file on its own. It's passed to the api and worker as a Docker secret (`/run/secrets/uploads_key`),
   so it doesn't appear in the environment or in `docker inspect`.

4. On start, the worker queues **Encrypt uploads**, which encrypts existing uploads and
   their previews in place (keeping their timestamps, so nothing is rescanned). Progress
   is under **Admin → Maintenance → Encryption of user uploads**. New uploads are
   encrypted as they stream in.

Without `UPLOADS_KEY_FILE` the secret is an empty file and encryption is off. A key file
that is unreadable or malformed stops the api and worker at startup with an explanation,
rather than silently storing uploads unencrypted.

**Don't** change or remove the key once files are encrypted: they would become
unreadable (playback and previews of those files fail with "encrypted with a different
uploads key"). Rotating the key isn't supported yet.

## Importing from Stash

Admin → Import reads a [Stash](https://github.com/stashapp/stash) database (`stash-go.sqlite`,
any schema version) and stages its tags, performers and studios for review. Nothing is
created until an admin approves it: per kind, either everything or a ticked selection.
Rejecting the import discards the staged data.

- Upload the file, or put a copy in the media store and give its path. Use a Stash backup
  (or stop Stash before copying). The file is opened read-only and not kept.
- Items whose name already exists here are matched and left untouched.
- Carried over: tag descriptions, aliases (appended to the description) and hierarchy;
  performer details, aliases, tags and favourites (they become the importing admin's);
  studio parents, URL, details and rating.
- Pictures are not taken from Stash. "Pictures from a folder" on the same page assigns
  images from a folder in the media store by file name (`Some Studio.png` → "Some Studio").
  Keep that folder outside your libraries, e.g. `/media/_studio-images`.
- Scene/image assignments, markers and play history are not imported.

## Resource use

Scans and preview generation run in `crypttube-worker`, which is deliberately gentle:
it runs at low CPU priority, its ffmpeg/ffprobe helpers at the lowest CPU and I/O priority
with one decoder thread each, and it reads only a few files at a time. The container is
also capped (`WORKER_CPUS`, default 2; `WORKER_MEMORY`, default 2g). To index faster on a
strong server raise those together with `SCAN_PROBE_CONCURRENCY` (files read at once,
default 4) and `GENERATE_CONCURRENCY` (scenes getting previews at once, default 2); on slow
or network storage, lower the two concurrency values.

## Duplicates

Admin → Duplicates lists files with identical content under different names (scenes and
images in global libraries; zip members and users' own libraries are not compared). It
uses the fingerprint stored at index time (size + hash of the first and last 64 KiB), so
it costs no disk reads. "Combine details" gives every copy the tags, performers and other
details of the others. "Delete" removes one copy, "Delete all extra copies" (a background
job) removes all but the first-indexed copy of every set. Before any copy is deleted its
details are combined into the ones that stay and it is compared byte for byte with one of
them; a file that only shares the fingerprint is kept.

Admins can also delete any scene, image or gallery in a global library from its page or
from a selection (file, database entries and generated content go together). Users'
own libraries are theirs to manage.

Jobs share the worker like this: scans (and cleanup, nightly maintenance and duplicate
removal) read the disk hard and always run on their own; preview generation runs as
batches, several at once, up to the number set on Admin → Jobs (default 1). A waiting scan
goes first: no new preview batch starts until it has run.

## Maintenance

Admin → Maintenance.

**Clean up removed media.** Files deleted outside the app stay in the database, marked
missing, so their metadata survives if they come back. A cleanup rescans the libraries
(moved or renamed files are followed, not removed), then deletes the entries of files that
are really gone and the generated files left behind. "Check" is a dry run that only
reports. Safeguards: a library that is disabled or whose folder isn't there (an unmounted
drive) is skipped; a file marked missing but present on disk is kept; and nothing is
removed while files are still being copied. Originals are never touched.

**Unreadable files** (main libraries) are listed with the reason in plain English, e.g.
"Incomplete video: its index is missing" for ffprobe's "moov atom not found". Scans skip
them until they change. Each can be tried again (after fixing it), ignored, or deleted.
Unreadable files in users' own libraries show as "not indexed" on their Files tab.

**Routine maintenance** runs once a day (default 04:00, in the time zone given by `TZ`),
or on demand:

- deletes expired sessions and old job records (finished: 7 days, failed: 30 days);
- deletes generated files nothing refers to: previews of scenes and thumbnails of images
  that no longer exist, pictures of deleted performers/studios/tags, stale temp files;
- optionally forgets files that have been missing for more than N days (off by default);
- runs `VACUUM (ANALYZE)` on every table, which reclaims dead rows and refreshes the
  planner statistics. It does not block readers or writers.

## Access control

Three layers, all applied inside SQL by one clause builder (`app/acl.py`): library
visibility (above), personal metadata (above), and per-user deny rules.
Per-user deny rules (`user_hidden_tags/studios/performers`) are expanded once per
request (sub-tags, child studios, performers carrying a hidden tag) and enforced inside
every list/detail query; hidden items 404 and don't appear in counts.

Each user can also set their own **content filters** on the Account page: tags and
performers they don't want to see. These are applied exactly like restrictions (a tag
covers the tags under it), for that user only and for every role, and can be switched off
without losing the selection. The opposite is there too: with "Only show" tags or
performers chosen, a user sees only scenes, galleries and images that have at least one
of them (hiding wins when both apply).

## Design invariants: no feedback loops

Automated components must never be able to trigger each other indefinitely.
Every new automated behaviour must preserve these rules:

1. **Watcher output never re-enters watcher input.** The file watcher observes only
   configured library paths under `/media`, where the app writes nothing (it only deletes, on an admin's request). All generated
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
