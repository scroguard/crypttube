# Handoff

## Current status
- `main` @ `349fff8`: all features to date (see README), including Unreadable files and scoped search.
- Branch `review-fixes`: fixes for the 15 findings of CodeRabbit's full-repository review
  (PR #1, `full-review` → `review-base`, review-only). Backend 313 passed; e2e 40 passed
  before the last small follow-up (transcode slot release on source-open failure, live-clip
  temp file only for unencrypted output), which backend tests cover.
- Not yet merged to `main`.

## Next steps
1. Merge `review-fixes` into `main` and push; redeploy with a rebuild.
2. Behind a reverse proxy, set `TRUSTED_PROXY` (the proxy's address or network, CIDR) on the
   stack so logins are throttled per visitor (see README, "Reaching it from elsewhere").
3. Close PR #1 and delete `full-review` and `review-base`:
   `gh pr close 1 --delete-branch && git push origin --delete review-base`.
4. Known limits: no key rotation for upload encryption; performer pictures taken from private
   uploads aren't encrypted; uploads aren't resumable.

## Context & architecture
- Everything runs in Docker Compose. `compose.yaml` stands alone (Dockhand stack);
  `compose.dev.yaml` is opt-in; `compose.e2e.yaml` is project `goon-e2e`. Container names
  are fixed (`goonstreamer-*`), so only one stack runs at a time.
- Tests: `docker compose up -d goonstreamer-db; docker compose run --rm goonstreamer-api pytest -q`;
  `scripts/e2e.sh`; vitest in `node:22-alpine`. Rebuild `goonstreamer-backend:dev` if a
  production build overwrote it.
- nginx config is an image template (`/etc/nginx/templates/`): `${TRUSTED_PROXY}` is filled
  in at start; other `$vars` are nginx's own. nginx sends the API a single
  `X-Forwarded-For` (the resolved visitor address); uvicorn trusts it (`FORWARDED_ALLOW_IPS=*`).
- Transcode slots: `TranscodeLimiter.acquire()` in the route; released by the stream, the
  route's body generator, or the response's background task (release is idempotent).
- Full-repo CodeRabbit review: an empty orphan base branch plus a snapshot branch on top of
  it, then comment `@coderabbitai full review` on the PR (a branch identical to `main` gives
  no diff).
