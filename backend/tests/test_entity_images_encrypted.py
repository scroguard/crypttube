"""Performer/studio/tag pictures are stored encrypted whenever an uploads key is set (one may
come from a private upload), served decrypted in memory, and swept by encrypt_uploads."""

import io

import pytest
from PIL import Image

from app import crypto, worker
from app.config import get_settings
from app.jobs import JobContext
from tests.test_crypto import KEY, on_disk_encrypted
from tests.test_entity_images import png
from tests.test_user_libraries import alice_lib, drain, people, upload  # noqa: F401  (fixtures)


@pytest.fixture
def key():
    crypto.set_key(KEY)
    yield KEY
    crypto.set_key(None)


def files_of(kind: str, entity_id: int):
    root = get_settings().generated_dir / "entities" / kind
    return [root / f"{entity_id}.webp", root / f"{entity_id}.src.webp"]


async def shows(c, kind: str, entity_id: int) -> Image.Image:
    url = (await c.get(f"/api/{kind}/{entity_id}")).json()["image_url"]
    r = await c.get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    im = Image.open(io.BytesIO(r.content))
    assert im.format == "WEBP"
    return im


async def test_a_picture_from_a_private_upload_is_encrypted(app, people, alice_lib, key):
    alice = people["alice"]
    hers = (await alice.get(f"/api/galleries/{alice_lib['gallery']}/images")).json()["items"][0]["id"]
    mine = (await alice.post("/api/performers", json={"name": "Hers"})).json()["id"]
    assert (await alice.post(f"/api/performers/{mine}/image/from-library", json={"image_id": hers})).status_code == 204
    assert all(p.exists() and on_disk_encrypted(p) for p in files_of("performers", mine))
    await shows(alice, "performers", mine)
    # the crop tool's original, and a crop, still work, and stay encrypted
    src = await alice.get(f"/api/performers/{mine}/image/source")
    assert src.status_code == 200 and Image.open(io.BytesIO(src.content)).format == "WEBP"
    assert (await alice.post(f"/api/performers/{mine}/image/crop",
                             json={"x": 0, "y": 0, "w": 0.5, "h": 1})).status_code == 204
    assert all(on_disk_encrypted(p) for p in files_of("performers", mine))
    await shows(alice, "performers", mine)
    # from a scene's screenshot too
    tag = (await alice.post("/api/tags", json={"name": "Her tag"})).json()["id"]
    assert (await alice.post(f"/api/tags/{tag}/image/from-library", json={"scene_id": alice_lib["scene"]})).status_code == 204
    assert all(on_disk_encrypted(p) for p in files_of("tags", tag))
    await shows(alice, "tags", tag)


async def test_every_picture_is_encrypted_with_a_key(client, app, clean, key):
    perf = (await client.post("/api/performers", json={"name": "Anyone"})).json()["id"]
    assert (await client.put(f"/api/performers/{perf}/image", content=png((300, 200)))).status_code == 204
    assert all(on_disk_encrypted(p) for p in files_of("performers", perf))
    assert (await shows(client, "performers", perf)).size == (300, 200)


async def test_without_a_key_they_stay_as_they_are(client, app, clean):
    perf = (await client.post("/api/performers", json={"name": "Plain"})).json()["id"]
    assert (await client.put(f"/api/performers/{perf}/image", content=png())).status_code == 204
    assert not any(on_disk_encrypted(p) for p in files_of("performers", perf))
    await shows(client, "performers", perf)


async def test_existing_plain_pictures_are_swept(client, app, clean):
    pool = app.state.pool
    perf = (await client.post("/api/performers", json={"name": "Before"})).json()["id"]
    assert (await client.put(f"/api/performers/{perf}/image", content=png())).status_code == 204   # no key yet
    crypto.set_key(KEY)
    try:
        job_id = await pool.fetchval("INSERT INTO jobs (kind, status) VALUES ('encrypt_uploads', 'running') RETURNING id")
        msg = await worker.handle_encrypt_uploads(pool, get_settings(), JobContext(pool, job_id), {"scheduled": True})
        assert msg == "encrypted 0 uploaded files and 2 generated files"
        assert all(on_disk_encrypted(p) for p in files_of("performers", perf))
        await shows(client, "performers", perf)
    finally:
        crypto.set_key(None)
