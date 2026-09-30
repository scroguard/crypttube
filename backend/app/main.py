import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health, jobs, libraries
from app.config import get_settings
from app.db import create_pool, migrate

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.generated_dir.mkdir(parents=True, exist_ok=True)
    pool = await create_pool(settings.database_url, settings.db_pool_min, settings.db_pool_max)
    if settings.run_migrations:
        await migrate(pool)
    app.state.pool = pool
    yield
    await pool.close()


app = FastAPI(
    title="goon-streamer",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)
for r in (health.router, libraries.router, jobs.router):
    app.include_router(r, prefix="/api")
