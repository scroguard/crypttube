import os

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

TEST_DB = "goon_test"


def _dsn_for(dbname: str) -> str:
    base, _, _ = os.environ["DATABASE_URL"].rpartition("/")
    return f"{base}/{dbname}"


@pytest.fixture(scope="session", autouse=True)
async def test_database():
    """Create a fresh database for the test session and point the app at it."""
    admin = await asyncpg.connect(_dsn_for("postgres"))
    await admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    await admin.execute(f"CREATE DATABASE {TEST_DB}")
    os.environ["DATABASE_URL"] = _dsn_for(TEST_DB)
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    await admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    await admin.close()


@pytest.fixture(scope="session")
async def app(test_database):
    from app.main import app, lifespan

    async with lifespan(app):
        yield app


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def conn(app):
    """A connection inside a transaction that is rolled back after each test."""
    async with app.state.pool.acquire() as c:
        tx = c.transaction()
        await tx.start()
        yield c
        await tx.rollback()
