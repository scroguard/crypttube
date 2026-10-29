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
- Theming, three stacked PRs awaiting approval (merge in order; rebase each onto main after
  the one below merges): #5 roles, fonts, logo (`theming-roles`); #6 server stylesheet
  `CUSTOM_CSS_FILE` (`theming-custom-css`); #7 Admin → Theme editor (`theming-editor`).

## Next steps
1. Point Dockhand at `https://github.com/scroguard/crypttube`.
2. Server: stop the old stack *before* deploying the renamed one (two Postgres containers
   must never share `pgdata`), check volume names, then redeploy with a rebuild.
3. Behind a reverse proxy, set `TRUSTED_PROXY` (the proxy's own address) on the stack so
   logins are throttled per visitor (see README, "Reaching it from elsewhere").
4. Local cleanup (once no session runs from the old path): `rm "/var/home/claude/personal
   projects/goon-streamer"` (a link to `crypttube`) and
   `rm -r ~/.claude/projects/-var-home-claude-personal-projects-goon-streamer` (copied).
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
- CodeRabbit's GitHub app is on the Free plan (summary only). For line reviews, run the
  CLI: `coderabbit review --agent --committed --base main`.
- Full-repo CodeRabbit review: an empty orphan base branch plus a snapshot branch on top of
  it, then comment `@coderabbitai full review` on the PR (a branch identical to `main` gives
  no diff).
