# Handoff

## Current status
- `main` has every feature to date (see README), plus the fixes from CodeRabbit's
  full-repository review (PR #1, closed) and its review of those fixes (PR #2, merged).
  Backend 314 passed, e2e 41 passed.
- Renamed to CryptTube (PR #3, merged; repo is now `scroguard/crypttube`): containers, images, network and UI
  text. Kept on purpose: volume names (`goon-streamer_*`, pinned, `VOLUME_PREFIX`), the
  database user/name `goon` (fixed when Postgres first created it) and the upload key-id
  label in `crypto.key_id` (part of every encrypted file's header).

## Next steps
1. Point Dockhand at `https://github.com/scroguard/crypttube`.
2. Server: stop the old stack *before* deploying the renamed one (two Postgres containers
   must never share `pgdata`), check volume names, then redeploy with a rebuild.
3. Behind a reverse proxy, set `TRUSTED_PROXY` (the proxy's own address) on the stack so
   logins are throttled per visitor (see README, "Reaching it from elsewhere").
4. Known limits: no key rotation for upload encryption; performer pictures taken from private
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
- Full-repo CodeRabbit review: an empty orphan base branch plus a snapshot branch on top of
  it, then comment `@coderabbitai full review` on the PR (a branch identical to `main` gives
  no diff).
