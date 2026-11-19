"""Moving scenes and images to another folder from the web interface."""

import os
import shutil
from pathlib import Path

from PIL import Image

from tests.test_media_api import indexed  # noqa: F401  (fixture)
from tests.test_user_libraries import alice_lib, drain, people  # noqa: F401  (fixtures)


async def mv(c, kind, ids, dest, new_folder=None, **extra):
    body = {"kind": kind, "ids": ids, "dest": str(dest), "on_conflict": "skip", **extra}
    if new_folder:
        body["new_folder"] = new_folder
    return await c.post("/api/move", json=body)


async def test_move_images_into_a_new_folder_keeping_everything(app, client, indexed):
    pool = app.state.pool
    root = indexed["root"]
    pics = {Path(p).name: i for p, i in indexed["images"].items() if "/pics/" in p}
    a, b = pics["img1.jpg"], pics["img2.jpg"]
    tag = (await client.post("/api/tags", json={"name": "Moved"})).json()["id"]
    await client.patch(f"/api/images/{a}", json={"tag_ids": [tag], "rating": 80})
    thumb_before = (await client.get(f"/api/images/{a}/thumbnail")).content
    old_gallery = await pool.fetchrow("SELECT id, image_count FROM galleries WHERE folder_path = $1", str(root / "pics"))

    r = await mv(client, "images", [a, b], root, new_folder="Performer X/set 1")
    assert r.status_code == 200, r.text
    assert r.json()["moved"] == 2 and r.json()["skipped"] == []
    dest = root / "Performer X" / "set 1"
    assert r.json()["dest"] == str(dest)
    assert sorted(p.name for p in dest.iterdir()) == ["img1.jpg", "img2.jpg"]
    assert not (root / "pics" / "img1.jpg").exists() and (root / "pics" / "img10.jpg").exists()

    # same entry, same details, same previews
    img = (await client.get(f"/api/images/{a}")).json()
    assert img["file"]["path"] == str(dest / "img1.jpg") and img["rating"] == 80
    assert [t["name"] for t in img["tags"]] == ["Moved"]
    assert (await client.get(f"/api/images/{a}/thumbnail")).content == thumb_before
    # folder galleries follow
    new_gallery = await pool.fetchrow("SELECT id, image_count, title FROM galleries WHERE folder_path = $1", str(dest))
    assert new_gallery["image_count"] == 2 and new_gallery["title"] == "set 1"
    assert await pool.fetchval("SELECT image_count FROM galleries WHERE id = $1", old_gallery["id"]) == old_gallery["image_count"] - 2
    assert [g["id"] for g in img["galleries"]] == [new_gallery["id"]]

    # a rescan finds nothing to change
    from app.scanner import Scanner
    from app.config import get_settings
    lid = await pool.fetchval("SELECT id FROM library_paths WHERE path = $1", str(root))
    stats = await Scanner(pool, get_settings().generated_dir, settle_seconds=0).scan(lid)
    assert stats.summary() == "no changes"

    # moving again: already there; a name clash; a picture inside an archive
    assert (await mv(client, "images", [a], dest)).json()["skipped"][0]["reason"] == "it is already in that folder"
    shutil.copy(root / "pics" / "img10.jpg", dest / "img10.jpg")
    c = pics["img10.jpg"]
    assert "already in that folder" in (await mv(client, "images", [c], dest)).json()["skipped"][0]["reason"]
    assert (root / "pics" / "img10.jpg").exists()
    zipped = indexed["images"]["a.jpg"]
    assert (await mv(client, "images", [zipped], dest)).json()["skipped"] == [{"id": zipped, "reason": "it is inside an archive"}]


async def test_move_a_scene_to_another_main_library(app, client, indexed, tmp_path):
    pool = app.state.pool
    other = Path(os.environ["MEDIA_DIR"]) / "second-library"
    other.mkdir()
    try:
        lib2 = await pool.fetchval("INSERT INTO library_paths (path) VALUES ($1) RETURNING id", str(other))
        sid = indexed["scenes"]["vp9"]
        await client.patch(f"/api/scenes/{sid}", json={"rating": 60})
        r = await mv(client, "scenes", [sid], other)
        assert r.json()["moved"] == 1
        s = (await client.get(f"/api/scenes/{sid}")).json()
        assert s["file"]["path"] == str(other / "vp9.webm") and s["rating"] == 60
        assert await pool.fetchval("SELECT library_id FROM scenes WHERE id = $1", sid) == lib2
        assert (await client.get(f"/api/scenes/{sid}/stream", headers={"Range": "bytes=0-3"})).status_code == 206
    finally:
        shutil.rmtree(other, ignore_errors=True)


async def test_who_may_move_what(app, people, alice_lib, indexed):
    admin, alice, bob = people["admin"], people["alice"], people["bob"]
    pool = app.state.pool
    root = indexed["root"]
    main_scene = indexed["scenes"]["h264"]
    alice_root = await pool.fetchval("SELECT path FROM library_paths WHERE id = $1", alice_lib["id"])

    # where each can move things
    roots = lambda c: c.get("/api/move/folders")  # noqa: E731
    assert [e["path"] for e in (await roots(admin)).json()["entries"]] == [str(root)]
    assert [(e["name"], e["mine"]) for e in (await roots(alice)).json()["entries"]] == [("Alice's stuff", True)]
    assert (await roots(bob)).json()["entries"] == []
    assert (await alice.get("/api/move/folders", params={"path": str(root)})).status_code == 422
    assert (await admin.get("/api/move/folders", params={"path": alice_root})).status_code == 422
    listing = (await admin.get("/api/move/folders", params={"path": str(root)})).json()
    assert {"pics", "videos"} <= {e["name"] for e in listing["entries"]} and listing["parent"] is None

    # nobody crosses between the main libraries and a user's own
    assert (await mv(alice, "scenes", [main_scene], alice_root)).json()["skipped"][0]["reason"] == \
        "you can't move files in that library"
    assert (await mv(admin, "scenes", [main_scene], alice_root)).status_code == 422
    assert (await mv(alice, "scenes", [alice_lib["scene"]], root)).status_code == 422
    # bob can't even see alice's content
    assert (await mv(bob, "scenes", [alice_lib["scene"]], alice_root)).status_code == 422

    # alice moves her own video into a new folder of her library
    path = await pool.fetchval("SELECT f.path FROM scenes s JOIN files f ON f.id = s.file_id WHERE s.id = $1", alice_lib["scene"])
    r = await mv(alice, "scenes", [alice_lib["scene"]], alice_root, new_folder="clips")
    assert r.json()["moved"] == 1 and not os.path.exists(path)
    assert os.path.exists(os.path.join(alice_root, "clips", os.path.basename(path)))
    # her images out of their folder: the emptied folder goes, the gallery too
    images = [i["id"] for i in (await alice.get(f"/api/galleries/{alice_lib['gallery']}/images")).json()["items"]]
    album = os.path.dirname(await pool.fetchval(
        "SELECT f.path FROM images i JOIN files f ON f.id = i.file_id WHERE i.id = $1", images[0]))
    assert (await mv(alice, "images", images, alice_root)).json()["moved"] == 2
    assert not os.path.exists(album)
    assert await pool.fetchval("SELECT count(*) FROM galleries WHERE id = $1", alice_lib["gallery"]) == 0

    for bad in ("..", "../x", ".hidden", "a/../b"):
        assert (await mv(alice, "images", images, alice_root, new_folder=bad)).status_code == 422


async def test_a_live_photo_moves_with_its_video(app, client, clean, library_dir, sample_media):
    import subprocess
    pool = app.state.pool
    phone = library_dir / "phone"
    phone.mkdir()
    Image.new("RGB", (64, 48), "teal").save(phone / "IMG_7.JPG")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=duration=2:size=160x120:rate=15",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(phone / "IMG_7.MOV")], check=True)
    lid = await pool.fetchval("INSERT INTO library_paths (path) VALUES ($1) RETURNING id", str(library_dir))
    await pool.execute("INSERT INTO jobs (kind, payload) VALUES ('scan', $1)", {"library_id": lid, "settle": 0})
    await drain(app)
    img = (await client.get("/api/images", params={"q": "IMG_7"})).json()["items"][0]
    assert img["live_url"]
    r = await mv(client, "images", [img["id"]], library_dir, new_folder="Trip")
    assert r.json()["moved"] == 1
    assert sorted(p.name for p in (library_dir / "Trip").iterdir()) == ["IMG_7.JPG", "IMG_7.MOV"]
    again = (await client.get("/api/images", params={"q": "IMG_7"})).json()["items"][0]
    assert again["live_url"] and (await client.get(again["live_url"])).status_code == 200


async def test_name_clashes_ask_or_rename(app, client, indexed):
    root = indexed["root"]
    pics = {Path(p).name: i for p, i in indexed["images"].items() if "/pics/" in p}
    dest = root / "sorted"
    dest.mkdir()
    for n in ("img1.jpg", "img2.jpg", "img2 (2).jpg"):
        shutil.copy(root / "pics" / "img10.jpg", dest / n)

    # asked: nothing moves, each clash is reported with a free name to suggest
    r = await mv(client, "images", [pics["img1.jpg"], pics["img2.jpg"]], dest, on_conflict="ask")
    assert r.json()["moved"] == 0
    assert sorted((c["name"], c["suggested"]) for c in r.json()["conflicts"]) == [
        ("img1.jpg", "img1 (2).jpg"), ("img2.jpg", "img2 (3).jpg")]
    assert (root / "pics" / "img1.jpg").exists()

    # the user picks a name for one (the extension is kept), the other is numbered
    r = await mv(client, "images", [pics["img1.jpg"]], dest, on_conflict="ask", names={str(pics["img1.jpg"]): "first one.png"})
    assert r.json()["moved"] == 1 and (dest / "first one.jpg").exists()
    r = await mv(client, "images", [pics["img2.jpg"]], dest, on_conflict="rename")
    assert r.json()["moved"] == 1 and (dest / "img2 (3).jpg").exists()
    assert (await client.get(f"/api/images/{pics['img2.jpg']}")).json()["file"]["path"] == str(dest / "img2 (3).jpg")

    # a typed name that clashes again is asked about again; bad names are refused
    r = await mv(client, "images", [pics["img10.jpg"]], dest, on_conflict="ask", names={str(pics["img10.jpg"]): "img1"})
    assert r.json()["conflicts"][0]["name"] == "img1.jpg"
    for bad in ("../x", ".hidden", "a/b", "  "):
        assert (await mv(client, "images", [pics["img10.jpg"]], dest, names={str(pics["img10.jpg"]): bad})).status_code == 422


def test_two_moves_to_the_same_name_never_replace_a_file(tmp_path):
    """Two moves racing for one name: one wins, the other is told the name is taken; both files survive."""
    import threading
    from app.api.move import _move_file
    for round_ in range(50):
        d = tmp_path / str(round_)
        d.mkdir()
        a, b, dst = d / "a.mp4", d / "b.mp4", d / "same.mp4"
        a.write_bytes(b"A"), b.write_bytes(b"B")
        start, results = threading.Barrier(2), {}

        def go(src, key):
            start.wait()
            try:
                _move_file(str(src), str(dst))
                results[key] = "moved"
            except FileExistsError:
                results[key] = "taken"
        ts = [threading.Thread(target=go, args=(a, "a")), threading.Thread(target=go, args=(b, "b"))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(results.values()) == ["moved", "taken"]
        loser = a if results["a"] == "taken" else b
        assert loser.read_bytes() in (b"A", b"B") and dst.read_bytes() != loser.read_bytes()


def test_a_move_across_filesystems_copies_without_replacing(tmp_path, monkeypatch):
    import errno
    from app.api import move as m

    def no_link(*a, **kw):
        raise OSError(errno.EXDEV, "cross-device")
    monkeypatch.setattr(m.os, "link", no_link)
    src, dst = tmp_path / "a.mp4", tmp_path / "b.mp4"
    src.write_bytes(b"A")
    m._move_file(str(src), str(dst))
    assert dst.read_bytes() == b"A" and not src.exists()
    src.write_bytes(b"C")
    try:
        m._move_file(str(src), str(dst))
        raise AssertionError("replaced")
    except FileExistsError:
        pass
    assert dst.read_bytes() == b"A" and src.read_bytes() == b"C"


async def test_rename_mode_picks_the_next_name_if_another_move_takes_it(app, client, indexed, monkeypatch):
    """The name was free when checked but taken just before the move: with rename it moves under the next name."""
    from app.api import move as m
    root = indexed["root"]
    pics = {Path(p).name: i for p, i in indexed["images"].items() if "/pics/" in p}
    a = pics["img1.jpg"]
    dest = root / "racedir"
    dest.mkdir()
    real = m._move_to
    calls = []

    async def racing(pool, files, d, lib, stem):
        if not calls:                                    # another move grabs the name first
            (d / f"{stem}.jpg").write_bytes(b"theirs")
        calls.append(stem)
        return await real(pool, files, d, lib, stem)
    monkeypatch.setattr(m, "_move_to", racing)
    r = await mv(client, "images", [a], dest, on_conflict="rename")
    assert r.status_code == 200 and r.json()["moved"] == 1, r.text
    assert calls == ["img1", "img1 (2)"]
    assert (dest / "img1.jpg").read_bytes() == b"theirs" and (dest / "img1 (2).jpg").exists()


def _no_link(monkeypatch, err):
    import errno as E
    from app.api import move as m

    def fail(*a, **kw):
        raise OSError(getattr(E, err), err)
    monkeypatch.setattr(m.os, "link", fail)
    return m


def test_without_hard_links_a_taken_name_is_never_replaced(tmp_path, monkeypatch):
    """No hard links (FAT, protected_hardlinks): the fallback is an exclusive-create copy, not a rename."""
    m = _no_link(monkeypatch, "EPERM")
    src, dst = tmp_path / "a.mp4", tmp_path / "b.mp4"
    src.write_bytes(b"A"), dst.write_bytes(b"theirs")
    try:
        m._move_file(str(src), str(dst))
        raise AssertionError("replaced")
    except FileExistsError:
        pass
    assert dst.read_bytes() == b"theirs" and src.read_bytes() == b"A"
    dst.unlink()
    m._move_file(str(src), str(dst))
    assert dst.read_bytes() == b"A" and not src.exists()


def test_a_failed_copy_never_removes_someone_elses_file(tmp_path, monkeypatch):
    m = _no_link(monkeypatch, "EXDEV")
    dst = tmp_path / "b.mp4"
    dst.write_bytes(b"theirs")
    try:
        m._move_file(str(tmp_path / "gone.mp4"), str(dst))          # the source vanished
        raise AssertionError("moved")
    except FileNotFoundError:
        pass
    assert dst.read_bytes() == b"theirs"


def test_if_the_original_cant_be_removed_no_second_copy_is_left(tmp_path, monkeypatch):
    from app.api import move as m
    src, dst = tmp_path / "a.mp4", tmp_path / "b.mp4"
    src.write_bytes(b"A")
    real = m.os.unlink

    def stuck(p, *a, **kw):
        if str(p) == str(src):
            raise PermissionError(13, "read-only folder")
        return real(p, *a, **kw)
    monkeypatch.setattr(m.os, "unlink", stuck)
    try:
        m._move_file(str(src), str(dst))
        raise AssertionError("moved")
    except PermissionError:
        pass
    assert src.read_bytes() == b"A" and not dst.exists()


async def test_ask_mode_offers_a_name_when_another_move_takes_it(app, client, indexed, monkeypatch):
    from app.api import move as m
    root = indexed["root"]
    a = {Path(p).name: i for p, i in indexed["images"].items() if "/pics/" in p}["img1.jpg"]
    dest = root / "askdir"
    dest.mkdir()
    real = m._move_to

    async def racing(pool, files, d, lib, stem):
        (d / f"{stem}.jpg").write_bytes(b"theirs")
        return await real(pool, files, d, lib, stem)
    monkeypatch.setattr(m, "_move_to", racing)
    r = await mv(client, "images", [a], dest, on_conflict="ask")
    assert r.status_code == 200, r.text
    assert r.json()["conflicts"] and r.json()["conflicts"][0]["suggested"] == "img1 (2).jpg", r.text


def test_an_interrupted_copy_leaves_no_partial_file(tmp_path, monkeypatch):
    """CT-04: across filesystems the copy goes to a hidden temporary name; a failure mid-copy leaves nothing under
    the real name, and the temporary file is removed."""
    m = _no_link(monkeypatch, "EXDEV")
    src, dst = tmp_path / "a.mp4", tmp_path / "b.mp4"
    src.write_bytes(b"A" * (3 << 20))
    real = m.shutil.copyfileobj

    def dies(fin, fout, n):
        fout.write(fin.read(1 << 20))
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(m.shutil, "copyfileobj", dies)
    try:
        m._move_file(str(src), str(dst))
        raise AssertionError("moved")
    except OSError as e:
        assert e.errno == 28
    assert not dst.exists() and src.read_bytes() == b"A" * (3 << 20)
    assert [p.name for p in tmp_path.iterdir()] == ["a.mp4"]          # no temporary file left
    monkeypatch.setattr(m.shutil, "copyfileobj", real)


def test_the_copy_is_named_only_when_complete(tmp_path, monkeypatch):
    """The real name appears only after the bytes are written and synced (renameat2 NOREPLACE without links)."""
    m = _no_link(monkeypatch, "EXDEV")
    src, dst = tmp_path / "a.mp4", tmp_path / "b.mp4"
    src.write_bytes(b"A" * 1000)
    synced = []
    real_fsync = m.os.fsync

    def fsync(fd):
        synced.append(dst.exists())                                    # never named before its sync
        return real_fsync(fd)
    monkeypatch.setattr(m.os, "fsync", fsync)
    m._move_file(str(src), str(dst))
    assert synced and synced[0] is False
    assert dst.read_bytes() == b"A" * 1000 and not src.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["b.mp4"]


def test_no_replace_rename_refuses_a_taken_name(tmp_path):
    from app.api import move as m
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"new"), b.write_bytes(b"theirs")
    try:
        ok = m._rename_noreplace(str(a), str(b))
    except FileExistsError:
        ok = "refused"
    assert ok in ("refused", False)                                     # False: this filesystem can't
    assert b.read_bytes() == b"theirs"
