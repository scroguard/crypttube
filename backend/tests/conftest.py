import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

TEST_DB = "goon_test"


@pytest.fixture(scope="session", autouse=True)
async def test_database():
    """Create a fresh database for the test session and point the app at it."""
    from app.config import get_settings

    admin = await asyncpg.connect(**get_settings().db_params("postgres"))
    await admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    await admin.execute(f"CREATE DATABASE {TEST_DB}")
    os.environ["POSTGRES_DB"] = TEST_DB
    os.environ.pop("DATABASE_URL", None)
    scratch = Path(tempfile.mkdtemp(prefix="goon-test-"))
    os.environ["MEDIA_DIR"] = str(scratch / "media")
    os.environ["GENERATED_DIR"] = str(scratch / "generated")
    os.environ["UPLOADS_DIR"] = str(scratch / "uploads")
    (scratch / "media").mkdir()
    get_settings.cache_clear()
    yield
    await admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    await admin.close()
    shutil.rmtree(scratch, ignore_errors=True)


@pytest.fixture(scope="session")
async def app(test_database):
    from app.main import app, lifespan

    async with lifespan(app):
        yield app


TEST_ADMIN = "test-admin"


async def session_client(app, username: str, role: str = "viewer", password: str = "password123"):
    """An AsyncClient signed in as `username` (created if needed) via a real session cookie."""
    from app import auth
    pool = app.state.pool
    user_id = await pool.fetchval(
        """INSERT INTO users (username, password_hash, role) VALUES ($1, $2, $3)
           ON CONFLICT ((lower(username))) DO UPDATE SET role = EXCLUDED.role RETURNING id""",
        username, auth.hash_password(password), role)
    token = await auth.create_session(pool, user_id, "pytest")
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                    cookies={auth.COOKIE_NAME: token}, headers={"X-Requested-With": "test"})
    c.user_id = user_id
    return c


@pytest.fixture
async def client(app):
    """Signed in as an admin (admins bypass content restrictions)."""
    c = await session_client(app, TEST_ADMIN, "admin")
    async with c:
        yield c


@pytest.fixture
async def anon(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"X-Requested-With": "test"}) as c:
        yield c


@pytest.fixture
def login_as(app):
    """Factory: await login_as("bob", "viewer") -> signed-in client."""
    async def make(username: str, role: str = "viewer"):
        return await session_client(app, username, role)  # in-process: nothing to close

    return make


@pytest.fixture
async def conn(app):
    """A connection inside a transaction that is rolled back after each test."""
    async with app.state.pool.acquire() as c:
        tx = c.transaction()
        await tx.start()
        yield c
        await tx.rollback()


@pytest.fixture
async def clean(app):
    """Empty all data tables (for tests that write through the pool, not a rolled-back tx)."""
    await app.state.pool.execute(
        """TRUNCATE library_paths, files, scenes, images, galleries, studios, performers,
                    tags, jobs, entity_counts RESTART IDENTITY CASCADE"""
    )
    # Generated assets are keyed by content hash, so leftovers from one test would look
    # like "already generated" to the next.
    for d in (Path(os.environ["GENERATED_DIR"]), Path(os.environ["UPLOADS_DIR"])):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    # Keep the signed-in test admin (and its session) alive across cleans.
    await app.state.pool.execute("DELETE FROM users WHERE username <> $1", TEST_ADMIN)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def sample_media(test_database) -> Path:
    """Tiny real media files, generated once per session."""
    d = Path(tempfile.mkdtemp(prefix="goon-samples-"))
    src = ["-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=25",
           "-f", "lavfi", "-i", "sine=duration=1"]
    _ffmpeg(*src, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(d / "h264.mp4"))
    _ffmpeg(*src, "-c:v", "libx265", "-x265-params", "log-level=error", "-c:a", "aac", str(d / "h265.mkv"))
    _ffmpeg(*src, "-c:v", "libvpx-vp9", "-deadline", "realtime", "-c:a", "libopus", str(d / "vp9.webm"))
    from PIL import Image
    for i, color in enumerate(["red", "green", "blue"]):
        Image.new("RGB", (64 + i, 48), color).save(d / f"img{i}.jpg")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def library_dir(sample_media, tmp_path_factory) -> Path:
    """A populated library folder inside MEDIA_DIR:
       videos/{h264.mp4,h265.mkv,vp9.webm}, pics/{img2,img10,img1}.jpg, archive.zip (2 images)."""
    root = Path(tempfile.mkdtemp(dir=os.environ["MEDIA_DIR"]))
    (root / "videos").mkdir()
    for name in ("h264.mp4", "h265.mkv", "vp9.webm"):
        shutil.copy(sample_media / name, root / "videos" / name)
    (root / "pics").mkdir()
    for i, n in enumerate((2, 10, 1)):
        shutil.copy(sample_media / f"img{i}.jpg", root / "pics" / f"img{n}.jpg")
    with zipfile.ZipFile(root / "archive.zip", "w") as zf:
        zf.write(sample_media / "img0.jpg", "b.jpg")
        zf.write(sample_media / "img1.jpg", "a.jpg")
        zf.writestr("__MACOSX/._a.jpg", b"junk")
        zf.writestr("notes.txt", b"ignored")
    yield root
    shutil.rmtree(root, ignore_errors=True)
