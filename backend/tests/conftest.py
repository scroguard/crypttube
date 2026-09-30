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
    scratch = Path(tempfile.mkdtemp(prefix="goon-test-"))
    os.environ["MEDIA_DIR"] = str(scratch / "media")
    os.environ["GENERATED_DIR"] = str(scratch / "generated")
    (scratch / "media").mkdir()
    from app.config import get_settings

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


@pytest.fixture
async def clean(app):
    """Empty all data tables (for tests that write through the pool, not a rolled-back tx)."""
    await app.state.pool.execute(
        """TRUNCATE library_paths, files, scenes, images, galleries, studios, performers,
                    tags, jobs, users RESTART IDENTITY CASCADE"""
    )


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
