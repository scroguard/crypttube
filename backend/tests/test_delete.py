"""Admins deleting media for good: file, database entries, generated content."""

import errno
import os
import shutil

from app import deletion, generated, worker
from app.config import get_settings
from app.jobs import JobContext
from app.scanner import Scanner
from tests.test_media_api import indexed  # noqa: F401  (fixture)
from tests.test_user_libraries import alice_lib, people  # noqa: F401  (fixtures)


def gen_files() -> set[str]:
    root = get_settings().generated_dir
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


async def rescan(app):
    for r in await app.state.pool.fetch("SELECT id FROM library_paths WHERE owner_id IS NULL"):
        await Scanner(app.state.pool, get_settings().generated_dir, settle_seconds=0).scan(r["id"])


async def post(client, kind, ids):
    return await client.post("/api/delete", json={"kind": kind, "ids": ids})


async def test_delete_scene_image_and_galleries(app, client, indexed):
    pool = app.state.pool
    root = indexed["root"]
    sid = indexed["scenes"]["h264"]
    assert (await client.get(f"/api/scenes/{sid}/screenshot", params={"w": 320})).status_code == 200
    plain = next(i for p, i in indexed["images"].items() if p.endswith("pics/img10.jpg"))
    zipped = indexed["images"]["a.jpg"]
    for iid in indexed["images"].values():
        assert (await client.get(f"/api/images/{iid}/thumbnail")).status_code == 200
    before = gen_files()

    r = await post(client, "scenes", [sid, 999999])
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1 and r.json()["skipped"] == [{"id": 999999, "reason": "not found"}]
    assert r.json()["bytes"] > 0
    assert not (root / "videos" / "h264.mp4").exists() and (root / "videos" / "vp9.webm").exists()
    assert (await client.get(f"/api/scenes/{sid}")).status_code == 404
    assert await pool.fetchval("SELECT count(*) FROM files WHERE path LIKE '%h264.mp4'") == 0
    assert len(before - gen_files()) == 2            # its screenshot and the thumbnail made from it

    # a single image; one inside an archive can't be deleted on its own
    r = await post(client, "images", [plain, zipped])
    assert r.json()["deleted"] == 1
    assert r.json()["skipped"] == [{"id": zipped, "reason": "it is inside an archive; delete the gallery instead"}]
    assert not (root / "pics" / "img10.jpg").exists() and (root / "archive.zip").exists()
    assert len(before - gen_files()) == 3

    # galleries: the archive, and a folder gallery with every image in it
    galleries = {("zip" if g["zip_file_id"] else "folder"): g["id"]
                 for g in await pool.fetch("SELECT id, zip_file_id FROM galleries")}
    r = await post(client, "galleries", list(galleries.values()))
    assert r.json()["deleted"] == 2 and r.json()["skipped"] == []
    assert not (root / "archive.zip").exists() and not any((root / "pics").glob("*.jpg"))
    assert await pool.fetchval("SELECT count(*) FROM galleries") == 0
    assert await pool.fetchval("SELECT count(*) FROM images") == 0
    assert not [f for f in gen_files() if f.startswith("thumbs/images")]
    # nothing else was touched
    assert sorted(p.name for p in (root / "videos").iterdir()) == ["h265.mkv", "vp9.webm"]
    assert await pool.fetchval("SELECT count(*) FROM scenes") == 2


async def test_read_only_store_changes_nothing(app, client, indexed, monkeypatch):
    def refuse(path):
        raise OSError(errno.EROFS, "Read-only file system")
    monkeypatch.setattr(deletion.os, "unlink", refuse)
    sid = indexed["scenes"]["h264"]
    r = await post(client, "scenes", [sid])
    assert r.status_code == 409 and "MEDIA_MODE=rw" in r.json()["detail"]
    assert (await client.get(f"/api/scenes/{sid}")).status_code == 200
    assert (indexed["root"] / "videos" / "h264.mp4").exists()


async def test_shared_previews_survive_while_a_copy_remains(app, client, indexed):
    root = indexed["root"]
    shutil.copy(root / "videos" / "h264.mp4", root / "videos" / "copy.mp4")
    await rescan(app)
    a, b = [r["id"] for r in await app.state.pool.fetch(
        "SELECT s.id FROM scenes s JOIN files f ON f.id = s.file_id WHERE f.path ~ '(h264|copy).mp4$' ORDER BY f.path")]
    assert (await client.get(f"/api/scenes/{a}/screenshot")).status_code == 200
    before = gen_files()
    assert (await post(client, "scenes", [a])).json()["deleted"] == 1
    assert gen_files() == before                       # the other copy still uses them
    assert (await post(client, "scenes", [b])).json()["deleted"] == 1
    assert gen_files() == set()


async def test_only_admins_and_only_global_libraries(app, people, alice_lib):
    admin, alice, editor = people["admin"], people["alice"], people["editor"]
    for c in (alice, editor):
        assert (await post(c, "scenes", [alice_lib["scene"]])).status_code == 403
    # private: the admin can't even see it
    assert (await post(admin, "scenes", [alice_lib["scene"]])).json() == {
        "deleted": 0, "files": 0, "bytes": 0, "skipped": [{"id": alice_lib["scene"], "reason": "not found"}]}
    # shared with everyone: visible, but still the owner's to delete
    await alice.patch(f"/api/my/libraries/{alice_lib['id']}", json={"visibility": "everyone"})
    r = await post(admin, "scenes", [alice_lib["scene"]])
    assert r.json()["deleted"] == 0 and "user's own library" in r.json()["skipped"][0]["reason"]
    assert (await alice.get(f"/api/scenes/{alice_lib['scene']}")).status_code == 200


# --- duplicates -----------------------------------------------------------------------------

async def test_delete_duplicate_copies(app, client, indexed):
    pool = app.state.pool
    root = indexed["root"]
    (root / "other").mkdir()
    for name in ("b.mp4", "c.mp4"):
        shutil.copy(root / "videos" / "h264.mp4", root / "other" / name)
    await rescan(app)
    group = (await client.get("/api/duplicates")).json()["groups"][0]
    keeper = next(i for i in group["items"] if i["keep"])
    extras = [i for i in group["items"] if not i["keep"]]
    await client.patch(f"/api/scenes/{extras[0]['id']}", json={"rating": 60})

    # one copy: its details move to the others first, then the file goes
    r = await client.post("/api/duplicates/delete", json={"kind": "scene", "id": extras[0]["id"]})
    assert r.status_code == 200, r.text
    assert not os.path.exists(extras[0]["path"]) and os.path.exists(keeper["path"])
    assert (await client.get(f"/api/scenes/{keeper['id']}")).json()["rating"] == 60

    # A different file that merely has the same fingerprint is never deleted. Tried with the
    # look-alike both older and newer than the real copies: the job only ever deletes a
    # copy that is byte-identical to the one it keeps.
    fake = extras[1]
    vp9 = indexed["scenes"]["vp9"]
    await pool.execute("UPDATE files SET oshash = (SELECT oshash FROM files WHERE path = $1), size = (SELECT size FROM files WHERE path = $1) WHERE path = $2",
                       keeper["path"], str(root / "videos" / "vp9.webm"))
    r = await client.post("/api/duplicates/delete", json={"kind": "scene", "id": vp9})
    assert r.status_code == 409 and "not identical" in r.json()["detail"]
    assert (root / "videos" / "vp9.webm").exists()

    first = await pool.fetchval("SELECT f.path FROM files f WHERE f.oshash = (SELECT oshash FROM files WHERE path = $1) ORDER BY f.id LIMIT 1", keeper["path"])
    job_id = await pool.fetchval("INSERT INTO jobs (kind, status) VALUES ('delete_duplicates', 'running') RETURNING id")
    msg = await worker.HANDLERS["delete_duplicates"](pool, get_settings(), JobContext(pool, job_id), {"kind": "scene"})
    assert (root / "videos" / "vp9.webm").exists()
    # whichever is older, the two real copies agree with each other: one goes, the
    # look-alike stays (it matches nothing)
    assert msg.startswith("deleted 1 extra copies") and "1 kept because they differ from the other copies" in msg
    assert sum(os.path.exists(p) for p in (keeper["path"], fake["path"])) == 1
    assert first  # (the older copy may be either one)
    # the last real copy can't be removed through the duplicates tool
    await pool.execute("UPDATE files SET oshash = 'ffffffffffffffff' WHERE path LIKE '%vp9.webm'")
    keeper = (await client.get("/api/scenes")).json()["items"]
    keeper = next(s for s in keeper if s["id"] != vp9 and s["id"] != indexed["scenes"]["hevc"])
    r = await client.post("/api/duplicates/delete", json={"kind": "scene", "id": keeper["id"]})
    assert r.status_code == 409 and "only copy" in r.json()["detail"]

    assert (await client.post("/api/duplicates/delete-extras", json={"kind": "scene"})).status_code == 202


async def test_delete_all_extra_copies_of_one_set(app, client, indexed):
    root = indexed["root"]
    (root / "other").mkdir()
    for name in ("b.mp4", "c.mp4", "d.mp4"):
        shutil.copy(root / "videos" / "h264.mp4", root / "other" / name)
    shutil.copy(root / "videos" / "vp9.webm", root / "other" / "vp9 copy.webm")   # another set
    await rescan(app)
    groups = (await client.get("/api/duplicates")).json()["groups"]
    assert sorted(len(g["items"]) for g in groups) == [2, 4]
    four = next(g for g in groups if len(g["items"]) == 4)
    keeper = next(i for i in four["items"] if i["keep"])
    extra = next(i for i in four["items"] if not i["keep"])
    await client.patch(f"/api/scenes/{extra['id']}", json={"rating": 40})

    # asked through any copy of the set; the suggested keeper is the one that stays
    r = await client.post("/api/duplicates/delete-set", json={"kind": "scene", "id": extra["id"]})
    assert r.status_code == 200, r.text
    assert {k: r.json()[k] for k in ("deleted", "bytes", "kept", "reason", "suggested_kept")} == {
        "deleted": 3, "bytes": 3 * four["size"], "kept": 0, "reason": None, "suggested_kept": True}
    assert [os.path.exists(i["path"]) for i in four["items"]] == [i["keep"] for i in four["items"]]
    assert (await client.get(f"/api/scenes/{keeper['id']}")).json()["rating"] == 40
    # the other set is untouched
    left = (await client.get("/api/duplicates")).json()["groups"]
    assert len(left) == 1 and len(left[0]["items"]) == 2
    assert (root / "other" / "vp9 copy.webm").exists()
    assert (await client.post("/api/duplicates/delete-set", json={"kind": "scene", "id": 999999})).status_code == 404
    again = await client.post("/api/duplicates/delete-set", json={"kind": "scene", "id": keeper["id"]})
    assert again.json()["deleted"] == 0       # nothing left to delete in that set


async def test_delete_job_reports_progress_and_status(app, client, indexed, monkeypatch):
    """The comparison reads big pieces and reports how far it is; the Duplicates page
    can show the job's state and result."""
    from app.api import duplicates
    pool = app.state.pool
    root = indexed["root"]
    (root / "other").mkdir()
    shutil.copy(root / "videos" / "h264.mp4", root / "other" / "copy.mp4")
    await rescan(app)
    monkeypatch.setattr(duplicates, "COMPARE_CHUNK", 4096)       # several pieces even for a small file
    seen: list[str] = []
    real = JobContext.progress

    async def record(self, fraction, message=None):
        seen.append(message or "")
        self._last_progress = 0                                   # no throttling in the test
        await real(self, fraction, message)
    monkeypatch.setattr(JobContext, "progress", record)
    assert (await client.get("/api/duplicates/job")).json() is None
    r = await client.post("/api/duplicates/delete-extras", json={"kind": "scene"})
    job_id = r.json()["job_id"]
    await pool.execute("UPDATE jobs SET status = 'running' WHERE id = $1", job_id)
    msg = await worker.HANDLERS["delete_duplicates"](pool, get_settings(), JobContext(pool, job_id), {"kind": "scene"})
    assert msg.startswith("deleted 1 extra copies")
    assert any(m.startswith("set 1 of 1: compared") for m in seen)
    assert seen[-1] == "1 of 1 sets done · 1 deleted"
    await pool.execute("UPDATE jobs SET status = 'done', message = $2 WHERE id = $1", job_id, msg)
    status = (await client.get("/api/duplicates/job", params={"kind": "scene"})).json()
    assert status["id"] == job_id and status["status"] == "done" and status["message"] == msg
    assert (await client.get("/api/duplicates/job", params={"kind": "image"})).json() is None


async def test_identical_compares_every_byte(tmp_path):
    from app.api import duplicates
    a, b = tmp_path / "a", tmp_path / "b"
    data = bytes(range(256)) * 50_000
    a.write_bytes(data)
    b.write_bytes(data)
    assert (await duplicates.identical(str(a), str(b))).same
    b.write_bytes(data[:-1] + b"x")                 # differs in the last byte only
    r = await duplicates.identical(str(a), str(b))
    assert not r.same and r.reason == f"contents differ from byte {len(data) - 1:,}"
    b.write_bytes(data[:-1])                        # shorter
    assert "ends early" in (await duplicates.identical(str(a), str(b))).reason
    r = await duplicates.identical(str(a), str(tmp_path / "missing"))
    assert not r.same and r.reason.startswith("couldn't open") and "missing" in r.reason


async def test_identical_copes_with_short_reads(tmp_path, monkeypatch):
    """Network filesystems may return fewer bytes than asked without being at the end of
    the file. Identical files must still compare equal."""
    import builtins
    import random
    from app.api import duplicates
    a, b = tmp_path / "a", tmp_path / "b"
    data = random.Random(1).randbytes(3_000_000)
    a.write_bytes(data)
    b.write_bytes(data)
    monkeypatch.setattr(duplicates, "COMPARE_CHUNK", 1 << 20)
    real_open = builtins.open

    class Short:
        def __init__(self, f, seed):
            self.f, self.rng = f, random.Random(seed)

        def read(self, n=-1):           # like a network share: often less than asked
            return self.f.read(max(1, min(n, self.rng.randint(1, 300_000))))

        def close(self):
            self.f.close()

    seeds = iter(range(100))
    monkeypatch.setattr(builtins, "open", lambda p, m="r", *x, **k: Short(real_open(p, m), next(seeds)) if str(p).startswith(str(tmp_path)) else real_open(p, m, *x, **k))
    assert (await duplicates.identical(str(a), str(b))).same
    b_data = bytearray(data)
    b_data[2_500_000] ^= 1
    real_open(b, "wb").write(bytes(b_data))
    r = await duplicates.identical(str(a), str(b))
    assert not r.same and r.reason == "contents differ from byte 2,500,000"


async def test_set_keeps_the_copy_most_others_agree_with(app, client, indexed):
    """The first-indexed copy is damaged (a stretch of empty bytes); the other three agree.
    One of the three is kept, two are deleted, and the damaged one is left and reported."""
    import subprocess
    root = indexed["root"]
    # a video big enough that its middle lies outside the fingerprint (first/last 64 KiB)
    original = root / "videos" / "long.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=duration=4:size=640x360:rate=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", "2M", str(original)], check=True)
    good = original.read_bytes()
    assert len(good) > 400_000
    damaged = bytearray(good)
    middle = len(good) // 2
    damaged[middle:middle + 5000] = bytes(5000)         # what a broken copy often looks like
    original.write_bytes(bytes(damaged))                 # same size; ends unchanged -> same fingerprint
    await rescan(app)                                    # indexed first: the "Suggested to keep" copy
    (root / "other").mkdir()
    for name in ("b.mp4", "c.mp4", "d.mp4"):
        (root / "other" / name).write_bytes(good)
    await rescan(app)
    group = (await client.get("/api/duplicates")).json()["groups"][0]
    assert len(group["items"]) == 4
    first = next(i for i in group["items"] if i["keep"])
    assert first["path"] == str(original)

    r = await client.post("/api/duplicates/delete-set", json={"kind": "scene", "id": first["id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == 2 and body["kept"] == 1 and body["suggested_kept"] is False
    assert body["kept_copies"][0]["path"] == str(original)
    assert body["reason"].startswith(f"contents differ from byte {middle:,}")
    assert "this copy has empty bytes there, so it is probably damaged" in body["reason"]
    assert body["keeper"]["path"] in {str(root / "other" / n) for n in ("b.mp4", "c.mp4", "d.mp4")}
    left = sorted(p.name for p in (root / "other").iterdir())
    assert len(left) == 1 and original.exists()

    # on its own, the differing copy is refused unless forced...
    r = await client.post("/api/duplicates/delete", json={"kind": "scene", "id": first["id"]})
    assert r.status_code == 409 and "not identical" in r.json()["detail"]
    # ...and the set with the admin override removes it: one copy stays
    r = await client.post("/api/duplicates/delete-set", json={"kind": "scene", "id": first["id"], "force": True})
    assert r.status_code == 200 and r.json()["deleted"] == 1 and r.json()["kept"] == 0
    assert not original.exists() and len(list((root / "other").iterdir())) == 1


async def test_bulk_override_deletes_differing_copies_too(app, client, indexed):
    import subprocess
    pool = app.state.pool
    root = indexed["root"]
    good_path = root / "videos" / "long.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=duration=4:size=640x360:rate=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", "2M", str(good_path)], check=True)
    good = good_path.read_bytes()
    await rescan(app)
    bad = bytearray(good)
    bad[len(good) // 2] ^= 0xFF
    (root / "other").mkdir()
    (root / "other" / "bad.mp4").write_bytes(bytes(bad))
    await rescan(app)
    run = lambda payload: worker.HANDLERS["delete_duplicates"](  # noqa: E731
        pool, get_settings(), JobContext(pool, job_id), payload)
    job_id = await pool.fetchval("INSERT INTO jobs (kind, status) VALUES ('delete_duplicates', 'running') RETURNING id")
    msg = await run({"kind": "scene"})
    assert msg.startswith("deleted 0 extra copies") and "1 kept because they differ" in msg
    assert (root / "other" / "bad.mp4").exists()
    msg = await run({"kind": "scene", "force": True})
    assert msg.startswith("deleted 1 extra copies")
    assert good_path.exists() and not (root / "other" / "bad.mp4").exists()
    r = await client.post("/api/duplicates/delete-extras", json={"kind": "scene", "force": True})
    assert (await pool.fetchval("SELECT payload FROM jobs WHERE id = $1", r.json()["job_id"]))["force"] is True
