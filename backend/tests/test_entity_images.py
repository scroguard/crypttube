"""Setting performer/studio/tag images from a URL, the library, or the media store."""

import io

import httpx
import pytest
from PIL import Image

from app import fetch
from app.api import edit
from app.config import get_settings
from tests.test_media_api import indexed  # noqa: F401  (fixture)
from tests.test_user_libraries import alice_lib, people  # noqa: F401  (fixtures)


def png(size=(300, 200), color="green") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


# --- the URL fetcher (no real network: fake resolver + mock transport) -----------------

@pytest.mark.parametrize("ip,public", [
    ("93.184.216.34", True), ("2606:2800:220:1:248:1893:25c8:1946", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("172.16.0.9", False), ("192.168.1.10", False),
    ("169.254.169.254", False),   # cloud metadata
    ("100.64.0.1", False),        # CGNAT
    ("0.0.0.0", False), ("224.0.0.1", False),
    ("::1", False), ("fe80::1", False), ("fc00::1", False),
    ("::ffff:127.0.0.1", False), ("::ffff:10.0.0.1", False),
    ("not-an-ip", False),
])
def test_public_address_classification(ip, public):
    assert fetch.is_public_address(ip) is public


def resolver(mapping):
    async def resolve(host, port):
        return mapping[host]
    return resolve


async def test_fetch_connects_to_the_validated_address(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), host=request.headers["host"],
                    sni=request.extensions.get("sni_hostname"), cookie=request.headers.get("cookie"),
                    referer=request.headers.get("referer"))
        return httpx.Response(200, content=b"IMAGEBYTES")

    data = await fetch.fetch_image_bytes(
        "https://cdn.example.com/a/b.jpg?x=1", max_bytes=1000,
        resolve=resolver({"cdn.example.com": ["93.184.216.34"]}), transport=httpx.MockTransport(handler))
    assert data == b"IMAGEBYTES"
    assert seen["url"] == "https://93.184.216.34/a/b.jpg?x=1"     # pinned: no second DNS lookup
    assert seen["host"] == "cdn.example.com" and seen["sni"] == "cdn.example.com"
    assert seen["cookie"] is None and seen["referer"] is None


@pytest.mark.parametrize("url,addresses,reason", [
    ("http://intranet.local/x.png", ["192.168.1.5"], "public internet"),
    ("http://rebind.example/x.png", ["93.184.216.34", "127.0.0.1"], "public internet"),   # mixed = internal
    ("http://127.0.0.1:8000/api/health", None, "public internet"),
    ("http://[::1]/x.png", None, "public internet"),
    ("http://169.254.169.254/latest/meta-data/", None, "public internet"),
    ("file:///etc/passwd", None, "only http"),
    ("ftp://example.com/x.png", None, "only http"),
    ("https://user:pw@example.com/x.png", ["93.184.216.34"], "credentials"),
    ("not a url at all", None, "http"),
])
async def test_fetch_refuses_unsafe_targets(url, addresses, reason):
    def handler(request):
        raise AssertionError("no request may be sent")

    hosts = {"intranet.local": addresses, "rebind.example": addresses, "example.com": addresses}
    with pytest.raises(fetch.FetchError) as e:
        await fetch.fetch_image_bytes(url, max_bytes=1000, resolve=resolver(hosts),
                                      transport=httpx.MockTransport(handler))
    assert reason in str(e.value)


async def test_fetch_validates_every_redirect():
    hosts = {"a.example": ["93.184.216.34"], "b.example": ["93.184.216.35"], "evil.example": ["10.0.0.7"]}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        if host == "a.example":
            return httpx.Response(302, headers={"location": "https://b.example/final.png"})
        if host == "b.example" and request.url.path == "/final.png":
            return httpx.Response(200, content=b"OK")
        if host == "b.example":
            return httpx.Response(307, headers={"location": "http://evil.example/secret"})
        raise AssertionError(f"must never contact {host}")

    t = httpx.MockTransport(handler)
    assert await fetch.fetch_image_bytes("https://a.example/x", max_bytes=10, resolve=resolver(hosts), transport=t) == b"OK"
    with pytest.raises(fetch.FetchError, match="public internet"):
        await fetch.fetch_image_bytes("https://b.example/to-internal", max_bytes=10, resolve=resolver(hosts), transport=t)

    loop = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(fetch.FetchError, match="too many redirects"):
        await fetch.fetch_image_bytes("https://a.example/x", max_bytes=10, resolve=resolver(hosts), transport=loop)


async def test_fetch_limits_size_and_reports_errors():
    hosts = resolver({"a.example": ["93.184.216.34"]})
    big = httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(fetch.FetchError, match="too large"):
        await fetch.fetch_image_bytes("https://a.example/x", max_bytes=1000, resolve=hosts, transport=big)
    missing = httpx.MockTransport(lambda r: httpx.Response(404))
    with pytest.raises(fetch.FetchError, match="HTTP 404"):
        await fetch.fetch_image_bytes("https://a.example/x", max_bytes=1000, resolve=hosts, transport=missing)
    empty = httpx.MockTransport(lambda r: httpx.Response(200, content=b""))
    with pytest.raises(fetch.FetchError, match="empty"):
        await fetch.fetch_image_bytes("https://a.example/x", max_bytes=1000, resolve=hosts, transport=empty)


# --- endpoints -------------------------------------------------------------------------

@pytest.fixture
async def performer(client, clean):
    return (await client.post("/api/performers", json={"name": "Pic Person"})).json()["id"]


async def served_image(client, kind, entity_id):
    url = (await client.get(f"/api/{kind}/{entity_id}")).json()["image_url"]
    assert url, "no image set"
    r = await client.get(url)
    assert r.status_code == 200
    return Image.open(io.BytesIO(r.content))


async def test_image_from_url(client, performer, login_as, monkeypatch):
    calls = []

    async def fake_fetch(url, *, max_bytes):
        calls.append(url)
        if "broken" in url:
            raise fetch.FetchError("the server answered with HTTP 404")
        return b"<html>not an image</html>" if "html" in url else png()

    monkeypatch.setattr(edit, "fetch_image_bytes", fake_fetch)
    r = await client.post(f"/api/performers/{performer}/image/from-url", json={"url": "https://example.com/p.png"})
    assert r.status_code == 204, r.text
    im = await served_image(client, "performers", performer)
    assert im.format == "WEBP" and im.size == (300, 200)

    r = await client.post(f"/api/performers/{performer}/image/from-url", json={"url": "https://example.com/broken"})
    assert r.status_code == 422 and "HTTP 404" in r.text
    r = await client.post(f"/api/performers/{performer}/image/from-url", json={"url": "https://example.com/page.html"})
    assert r.status_code == 422 and "not a usable image" in r.text

    # the real fetcher's refusal reaches the user as a clear message
    monkeypatch.undo()
    r = await client.post(f"/api/performers/{performer}/image/from-url", json={"url": "http://127.0.0.1:8000/api/health"})
    assert r.status_code == 422 and "public internet" in r.text

    viewer = await login_as("vv", "viewer")
    r = await viewer.post(f"/api/performers/{performer}/image/from-url", json={"url": "https://example.com/p.png"})
    assert r.status_code == 403

    monkeypatch.setattr(get_settings(), "url_image_fetch", False)
    r = await client.post(f"/api/performers/{performer}/image/from-url", json={"url": "https://example.com/p.png"})
    assert r.status_code == 403 and "disabled" in r.text


async def test_image_from_library(client, indexed):
    perf = (await client.post("/api/performers", json={"name": "From Library"})).json()["id"]
    plain = next(i for p, i in indexed["images"].items() if p.endswith("pics/img10.jpg"))
    assert (await client.post(f"/api/performers/{perf}/image/from-library", json={"image_id": plain})).status_code == 204
    assert (await served_image(client, "performers", perf)).size == (65, 48)

    zipped = indexed["images"]["a.jpg"]   # a member of archive.zip
    studio = (await client.post("/api/studios", json={"name": "S"})).json()["id"]
    assert (await client.post(f"/api/studios/{studio}/image/from-library", json={"image_id": zipped})).status_code == 204
    assert (await served_image(client, "studios", studio)).format == "WEBP"

    tag = (await client.post("/api/tags", json={"name": "T"})).json()["id"]
    scene = indexed["scenes"]["h264"]
    assert (await client.post(f"/api/tags/{tag}/image/from-library", json={"scene_id": scene})).status_code == 204
    assert (await served_image(client, "tags", tag)).size == (320, 240)   # the scene's screenshot

    for bad in ({}, {"image_id": plain, "scene_id": scene}):
        assert (await client.post(f"/api/performers/{perf}/image/from-library", json=bad)).status_code == 422
    assert (await client.post(f"/api/performers/{perf}/image/from-library", json={"image_id": 999999})).status_code == 404


async def test_image_from_library_respects_visibility(app, people, alice_lib):
    """You can only use an image you can see: an id from someone's private library is a 404."""
    alice, editor = people["alice"], people["editor"]
    hers = (await alice.get(f"/api/galleries/{alice_lib['gallery']}/images")).json()["items"][0]["id"]
    target = (await editor.post("/api/performers", json={"name": "Global Person"})).json()["id"]
    r = await editor.post(f"/api/performers/{target}/image/from-library", json={"image_id": hers})
    assert r.status_code == 404
    assert (await editor.post(f"/api/performers/{target}/image/from-library",
                              json={"scene_id": alice_lib["scene"]})).status_code == 404
    # the owner can use her own images on her own (personal) performer
    mine = (await alice.post("/api/performers", json={"name": "Hers"})).json()["id"]
    assert (await alice.post(f"/api/performers/{mine}/image/from-library", json={"image_id": hers})).status_code == 204
    # ...and nobody else can change that performer's image
    assert (await editor.post(f"/api/performers/{mine}/image/from-library", json={"image_id": hers})).status_code == 404


async def test_image_from_media_store_is_admin_only(client, login_as, indexed):
    root = indexed["root"]
    (root / "loose").mkdir()
    Image.new("RGB", (120, 90), "orange").save(root / "loose" / "unindexed.png")   # never scanned
    (root / "loose" / "notes.txt").write_text("x")
    perf = (await client.post("/api/performers", json={"name": "Server Pick"})).json()["id"]
    url = f"/api/performers/{perf}/image/from-path"

    assert (await client.post(url, json={"path": str(root / "loose" / "unindexed.png")})).status_code == 204
    assert (await served_image(client, "performers", perf)).size == (120, 90)

    editor = await login_as("ed3", "editor")
    assert (await editor.post(url, json={"path": str(root / "loose" / "unindexed.png")})).status_code == 403
    assert (await client.post(url, json={"path": "/etc/hostname"})).status_code == 422
    assert (await client.post(url, json={"path": f"{root}/../../../etc/hostname"})).status_code == 422
    assert (await client.post(url, json={"path": str(root / "loose" / "missing.png")})).status_code == 404
    assert (await client.post(url, json={"path": str(root / "loose" / "notes.txt")})).status_code == 415
    assert (await client.post(url, json={"path": str(root / "videos" / "h264.mp4")})).status_code == 415


async def test_browse_lists_image_files_with_previews(client, login_as, indexed):
    root = indexed["root"]
    plain = (await client.get("/api/libraries/browse", params={"path": str(root / "pics")})).json()
    assert plain["files"] == []                                   # only when asked
    body = (await client.get("/api/libraries/browse", params={"path": str(root / "pics"), "images": True})).json()
    assert [f["name"] for f in body["files"]] == ["img1.jpg", "img2.jpg", "img10.jpg", "rotated.jpg"]
    assert body["files"][0]["size"] > 0
    videos = (await client.get("/api/libraries/browse", params={"path": str(root / "videos"), "images": True})).json()
    assert videos["files"] == []                                  # videos aren't offered as pictures

    r = await client.get("/api/libraries/browse/preview", params={"path": body["files"][0]["path"]})
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(r.content)).format == "WEBP"
    assert (await client.get("/api/libraries/browse/preview", params={"path": "/etc/hostname"})).status_code == 422
    editor = await login_as("ed4", "editor")
    assert (await editor.get("/api/libraries/browse/preview", params={"path": body["files"][0]["path"]})).status_code == 403


async def test_random_image_from_the_assigned_ones(app, client, indexed):
    perf = (await client.post("/api/performers", json={"name": "Random Pick"})).json()["id"]
    r = await client.post(f"/api/performers/{perf}/image/random")
    assert r.status_code == 404 and "no images are assigned" in r.json()["detail"]

    mine = [i for p, i in indexed["images"].items() if "pics/img" in p]          # 3 of the 6 images
    for iid in mine:
        assert (await client.patch(f"/api/images/{iid}", json={"performer_ids": [perf]})).status_code == 200
    seen, last = set(), None
    for _ in range(12):
        r = await client.post(f"/api/performers/{perf}/image/random", json={"exclude_image_id": last})
        assert r.status_code == 200, r.text
        pick = r.json()
        assert pick["image_id"] in mine and pick["image_id"] != last              # always a different one
        assert (await client.get(pick["thumbnail_url"])).status_code == 200
        seen.add(pick["image_id"])
        last = pick["image_id"]
    assert len(seen) > 1
    assert (await served_image(client, "performers", perf)).format == "WEBP"

    # a single assigned image: "again" simply gives the same one
    solo = (await client.post("/api/tags", json={"name": "Solo"})).json()["id"]
    await client.patch(f"/api/images/{mine[0]}", json={"tag_ids": [solo]})
    for _ in range(2):
        r = await client.post(f"/api/tags/{solo}/image/random", json={"exclude_image_id": mine[0]})
        assert r.json()["image_id"] == mine[0]
    # an image that has gone missing is never picked
    await app.state.pool.execute(
        "UPDATE files SET missing_since = now() WHERE id = (SELECT file_id FROM images WHERE id = $1)", mine[0])
    assert (await client.post(f"/api/tags/{solo}/image/random")).status_code == 404


async def test_random_image_needs_edit_rights(client, login_as, indexed):
    perf = (await client.post("/api/performers", json={"name": "Guarded"})).json()["id"]
    viewer = await login_as("vera", "viewer")
    assert (await viewer.post(f"/api/performers/{perf}/image/random")).status_code == 403


async def test_crop_a_performer_picture_and_recrop_without_loss(app, client, clean, login_as):
    from app.config import get_settings
    perf = (await client.post("/api/performers", json={"name": "Cropped"})).json()["id"]
    # a 1600x800 picture: left half red, right half blue
    im = Image.new("RGB", (1600, 800), "red")
    im.paste(Image.new("RGB", (800, 800), "blue"), (800, 0))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    assert (await client.put(f"/api/performers/{perf}/image", content=buf.getvalue())).status_code == 204
    whole = await served_image(client, "performers", perf)
    assert whole.size == (1024, 512)                         # the whole picture, as before
    src = await client.get(f"/api/performers/{perf}/image/source")
    assert src.status_code == 200 and Image.open(io.BytesIO(src.content)).size == (1600, 800)
    url_before = (await client.get(f"/api/performers/{perf}")).json()["image_url"]

    # keep the right quarter, 2:3 portrait-ish: all blue
    r = await client.post(f"/api/performers/{perf}/image/crop", json={"x": 0.75, "y": 0, "w": 0.25, "h": 1})
    assert r.status_code == 204
    url_after = (await client.get(f"/api/performers/{perf}")).json()["image_url"]
    assert url_after != url_before                            # browsers fetch the new one
    cut = await served_image(client, "performers", perf)
    assert cut.size == (400, 800) and cut.convert("RGB").getpixel((200, 400))[2] > 200
    # re-crop from the original: the left quarter is red again, at full resolution
    await client.post(f"/api/performers/{perf}/image/crop", json={"x": 0, "y": 0, "w": 0.25, "h": 1})
    cut = await served_image(client, "performers", perf)
    assert cut.size == (400, 800) and cut.convert("RGB").getpixel((200, 400))[0] > 200

    bad = await client.post(f"/api/performers/{perf}/image/crop", json={"x": 0.9, "y": 0, "w": 0.5, "h": 1})
    assert bad.status_code == 422
    viewer = await login_as("vera-crop", "viewer")
    assert (await viewer.post(f"/api/performers/{perf}/image/crop", json={"x": 0, "y": 0, "w": 1, "h": 1})).status_code == 403
    assert (await viewer.get(f"/api/performers/{perf}/image/source")).status_code == 403

    # the original survives maintenance; it goes with the picture
    from app import maintenance
    gen = get_settings().generated_dir
    report = await maintenance.sweep_generated(app.state.pool, gen, delete=True)
    assert report["entity_images"]["files"] == 0 and (gen / f"entities/performers/{perf}.src.webp").exists()
    assert (await client.delete(f"/api/performers/{perf}/image")).status_code == 204
    assert not (gen / f"entities/performers/{perf}.src.webp").exists()
    other = (await client.post("/api/performers", json={"name": "No picture"})).json()["id"]
    assert (await client.post(f"/api/performers/{other}/image/crop", json={"x": 0, "y": 0, "w": 1, "h": 1})).status_code == 404
