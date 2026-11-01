# Handoff

## Current status
- `main` has every feature to date (see README), plus the fixes from CodeRabbit's
  full-repository review (PR #1, closed) and its review of those fixes (PR #2, merged).
  Backend 314 passed, e2e 41 passed.
- Renamed to CryptTube (PR #3, merged; repo is now `scroguard/crypttube`): containers, images, network and UI
  text. Kept on purpose: volume names (`goon-streamer_*`, pinned, `VOLUME_PREFIX`), the
  database user/name `goon` (fixed when Postgres first created it) and the upload key-id
  label in `crypto.key_id` (part of every encrypted file's header).
- GPU transcoding (PR #4, merged): opt-in via `compose.vaapi.yaml` (AMD/Intel) or
  `compose.nvidia.yaml`. Untested on real hardware: no GPU here or on the server yet.
- Theming merged (#5 roles, fonts, logo; #6 server stylesheet; #7 Admin → Theme editor with
  Crypt, Crypt Lime and Ossuary presets).
- Deployed 2026-10-29: the Dockhand stack (named `cryptube`) runs the renamed app with the
  pre-rename volumes (`EXISTING_VOLUMES=true`, PR #8); the server stylesheet mount became
  opt-in (`compose.custom-css.yaml`, PR #9) because Dockhand can't mount repo files. The
  user confirmed the library is intact. Nothing in progress.

## Next steps
1. Server: remove the leftover pre-rename containers if any remain (`docker ps -a | grep
   goonstreamer`, then `docker rm <name>`; never their volumes).
2. Behind a reverse proxy, set `TRUSTED_PROXY` (the proxy's own address) on the stack so
   logins are throttled per visitor (see README, "Reaching it from elsewhere").
3. Local cleanup (once no session runs from the old path): `rm "/var/home/claude/personal
   projects/goon-streamer"` (a link to `crypttube`) and
   `rm -r ~/.claude/projects/-var-home-claude-personal-projects-goon-streamer` (copied).
4. Optional: `compose.custom-css.yaml` + `CUSTOM_CSS_FILE` for a server stylesheet
   (README, "Theme").
5. When a GPU is installed: follow README "Hardware transcoding", check the API log for
   `hardware encoding on: ...`, then try a transcode (player quality menu: 720p).
6. Known limits: no key rotation for upload encryption; performer pictures taken from private
   uploads aren't encrypted; uploads aren't resumable.

## Context & architecture
- Everything runs in Docker Compose. `compose.yaml` stands alone (Dockhand stack);
  `compose.dev.yaml` is opt-in; `compose.e2e.yaml` is project `crypttube-e2e`. Container names
  are fixed (`crypttube-*`), so only one stack runs at a time.
- Tests: `docker compose up -d crypttube-db; docker compose run --rm crypttube-api pytest -q`;
  `scripts/e2e.sh`; vitest in `node:22-alpine`. Rebuild `crypttube-backend:dev` if a
  production build overwrote it.
- nginx config is an image template (`/etc/nginx/templates/`): `${TRUSTED_PROXY}` is filled
  in at start; other `$vars` are nginx's own. nginx sends the API a single
  `X-Forwarded-For` (the resolved visitor address); uvicorn trusts it (`FORWARDED_ALLOW_IPS=*`).
- Transcode slots: `TranscodeLimiter.acquire()` in the route; `TranscodeResponse` closes the
  body (stopping ffmpeg) and then releases, whatever ends the response (release is idempotent).
- Dockhand runs Compose inside its own container (`/app/data/stacks/...`): builds work, but
  a bind mount or config/secret `file:` relative to the repo does not exist on the host.
  Every host file in `compose.yaml` must be an absolute path from env (or `/dev/null`);
  repo files go into images, or into opt-in override files. The one exception is
  `MEDIA_ROOT` (default `./media`, for local use), which a Dockhand stack must set.
- Pre-rename installs whose Compose refuses the old-labelled volumes set `EXISTING_VOLUMES=true`
  (volumes become external). The user's Dockhand stack needs it.
- Volumes are named explicitly (`${VOLUME_PREFIX:-goon-streamer}_*`), so the Compose
  project name no longer decides them; `compose.e2e.yaml` names its own (`crypttube-e2e_*`), so a
  hand-run `down -v` there can't touch real data.
- GPU transcoding: only the encode runs on the GPU (`streaming._encoder`); decode/scale
  stay on the CPU so any source works. `detect_hw_accel` test-encodes at API startup and
  stays on the CPU if it fails; `ffmpeg_stream` retries a GPU stream on the CPU if it
  fails before any output. Live Photo clips stay on the CPU. The image carries the VAAPI
  drivers (mesa, Intel iHD from non-free); NVIDIA's come from the host's container toolkit.
- Theme layers, lowest first: built-in Crypt colors in index.css (Tailwind `@layer theme`),
  the admin's saved theme (`/api/theme.css`, unlayered, in <head>, stored in system_state
  key `theme`), then `/custom.css` (end of <body>). Presets live only in
  `frontend/src/lib/themes.ts`; a vitest checks index.css matches Crypt and every preset
  passes the contrast rules in `lib/contrast.ts`.
- Theme: design decisions come from Ink, the user's branding agent (via the "jarvis"
  session, which relays both ways). Roles, preset values and UI rules: Ink's handoff v0.1
  and update v0.2 (presets Crypt, Crypt Lime, Ossuary; dark only). Brand source files are in
  `items from ink/` (untracked, excluded locally in `.git/info/exclude`).
- Unlinking with "remove tags": `folder_link_tags` records what each link added; triggers
  drop a record when its tag goes, so a tag re-added by hand counts as hand-set. A tag with
  no record is never claimed by tagging; `older=true` on the unlink claims the folder's
  unrecorded tags (except those another link covers) in the request, so the job takes
  exactly what was there then. `folder_links.removing_at` stops tagging at once; the job
  deletes the link when it's done, and asking again resumes it.
- "Select all matching": `GET /api/ids/*` also returns `skipped` (visible but not editable);
  `lib/selection.ts` writes the note, which is tied to the selection Set it describes.
- Scene and image list items carry `can_edit`. Bulk edit is all-or-nothing per request (403
  if any item isn't editable), but the client sends chunks of 5,000. Cards lock what can't
  be edited while selecting, so a click or box can't pick it.
- Watched folders: `app/folder_links.py` `apply` is the only writer (scanner, Move, backfill
  job). It runs in the indexing transaction, so a failure leaves the file "new" for the next
  scan. Links are checked against their creator's rights at tag time.
- CodeRabbit's GitHub app is on the Free plan (summary only). For line reviews, run the
  CLI: `coderabbit review --agent --committed --base main`.
- Full-repo CodeRabbit review: an empty orphan base branch plus a snapshot branch on top of
  it, then comment `@coderabbitai full review` on the PR (a branch identical to `main` gives
  no diff).
