import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request

from app.api import (browse, delete, move, duplicates, edit, entities, filters, groups, health, home, jobs, libraries, maintenance, media, preferences,
                     my_libraries, session, stash, users)
from app.auth import require_admin, require_user
from app import crypto
from app.config import get_settings
from app.db import create_pool, migrate
from app.streaming import TranscodeLimiter

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    crypto.load_key(settings.uploads_key_file)   # a malformed key stops the API here, loudly
    settings.generated_dir.mkdir(parents=True, exist_ok=True)
    pool = await create_pool(settings.db_params(), settings.db_pool_min, settings.db_pool_max)
    if settings.run_migrations:
        await migrate(pool)
    app.state.pool = pool
    app.state.transcodes = TranscodeLimiter(settings.max_transcodes)
    yield
    await pool.close()


app = FastAPI(
    title="CryptTube",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)
# Every router is mounted with the minimum role it needs; only health and the auth
# endpoints (which do their own checks) are reachable without a session.
ROUTES = [
    ((health.router, session.router), []),
    ((browse.router, entities.router, groups.router, media.router, edit.read_router, edit.router,
      my_libraries.router, home.router, filters.router, preferences.router, delete.router, move.router),
     [Depends(require_user)]),
    ((libraries.router, jobs.router, users.router, stash.router, maintenance.router, duplicates.router), [Depends(require_admin)]),
]
for routers, deps in ROUTES:
    for r in routers:
        app.include_router(r, prefix="/api", dependencies=deps)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",  # never leak URLs (titles, ids) to other sites
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    # Metadata responses must not land in disk caches; media routes set their own policy.
    response.headers.setdefault("Cache-Control", "no-store")
    return response
