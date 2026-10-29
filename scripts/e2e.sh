#!/usr/bin/env bash
# End-to-end UI tests against a throwaway stack (project "goon-e2e").
# Usage: scripts/e2e.sh [--keep]   (--keep leaves the stack up for inspection)
set -euo pipefail
cd "$(dirname "$0")/.."

MEDIA=frontend/e2e/media/lib
if [ ! -f "$MEDIA/.done" ]; then
  echo "generating fixture media…"
  mkdir -p "$MEDIA/videos" "$MEDIA/pics"
  ff() { ffmpeg -v error -y "$@"; }
  SRC=(-f lavfi -i testsrc2=duration=20:size=640x360:rate=30 -f lavfi -i sine=frequency=440:duration=20)
  ff "${SRC[@]}" -c:v libx264 -pix_fmt yuv420p -g 30 -c:a aac "$MEDIA/videos/Beach Day.mp4"
  ff "${SRC[@]}" -c:v libvpx-vp9 -deadline realtime -g 30 -c:a libopus "$MEDIA/videos/City Lights.webm"
  ff -f lavfi -i testsrc=duration=10:size=640x360:rate=25 -c:v libx265 -x265-params log-level=error "$MEDIA/videos/Night Drive.mkv"
  for i in $(seq 1 12); do
    ff -f lavfi -i "testsrc2=size=800x600:rate=1:duration=1" -vf "hue=h=$((i * 30))" -frames:v 1 "$MEDIA/pics/photo$i.jpg"
  done
  python3 - "$MEDIA" <<'PY'
import sys, zipfile, pathlib
root = pathlib.Path(sys.argv[1])
with zipfile.ZipFile(root / "Holiday Set.zip", "w") as z:
    for i in (1, 2, 3):
        z.write(root / "pics" / f"photo{i}.jpg", f"set{i}.jpg")
PY
  touch "$MEDIA/.done"
fi

# A video whose MP4 carries DRM headers left over from a download (the content itself is
# not encrypted). Firefox refuses such files; it lives outside the main library.
DRM=frontend/e2e/media/drm
if [ ! -f "$DRM/Leftover Headers.mp4" ]; then
  mkdir -p "$DRM"
  python3 scripts/add_drm_headers.py "$MEDIA/videos/Beach Day.mp4" "$DRM/Leftover Headers.mp4"
fi

# A Live Photo (still + short .MOV with uncompressed audio, like an iPhone's), outside the
# main library.
LIVE=frontend/e2e/media/livephotos
if [ ! -f "$LIVE/IMG_LIVE.mov" ]; then
  mkdir -p "$LIVE"
  ffmpeg -v error -y -f lavfi -i "testsrc2=size=480x360:rate=1:duration=1" -frames:v 1 "$LIVE/IMG_LIVE.JPG"
  ffmpeg -v error -y -f lavfi -i "testsrc2=duration=2:size=480x360:rate=30" -f lavfi -i "sine=duration=2" \
    -c:v libx264 -pix_fmt yuv420p -c:a pcm_s16le "$LIVE/IMG_LIVE.mov"
fi

# A small Stash database and a studio picture, outside the library, for the import test.
STASH=frontend/e2e/media/stash
if [ ! -f "$STASH/stash-go.sqlite" ]; then
  mkdir -p "$STASH" frontend/e2e/media/_studio-images
  cp "$MEDIA/pics/photo4.jpg" "frontend/e2e/media/_studio-images/velvet works.jpg"
  python3 - "$STASH/stash-go.sqlite" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.executescript("""
CREATE TABLE schema_migrations (version uint64, dirty bool);
INSERT INTO schema_migrations VALUES (85, 0);
CREATE TABLE tags (id integer PRIMARY KEY, name varchar(255), description text);
CREATE TABLE tags_relations (parent_id integer, child_id integer);
CREATE TABLE performers (id integer PRIMARY KEY, name varchar(255), disambiguation varchar(255),
    gender varchar(20), birthdate date, country varchar(255), height int, favorite boolean, rating tinyint);
CREATE TABLE performer_aliases (performer_id integer, alias varchar(255));
CREATE TABLE performers_tags (performer_id integer, tag_id integer);
CREATE TABLE performers_scenes (performer_id integer, scene_id integer);
CREATE TABLE studios (id integer PRIMARY KEY, name varchar(255), parent_id integer, details text, rating tinyint);
INSERT INTO tags VALUES (1, 'Stash Setting', 'Where'), (2, 'Stash Rooftop', NULL), (3, 'Stash Unused', NULL);
INSERT INTO tags_relations VALUES (1, 2);
INSERT INTO performers VALUES (1, 'Imported Ivy', NULL, 'FEMALE', '1991-02-03', 'CA', 170, 1, 80),
                              (2, 'Imported Omar', NULL, 'MALE', NULL, NULL, NULL, 0, NULL);
INSERT INTO performer_aliases VALUES (1, 'Ivy I');
INSERT INTO performers_tags VALUES (1, 2);
INSERT INTO performers_scenes VALUES (1, 1), (1, 2), (2, 1);
INSERT INTO studios VALUES (1, 'Velvet Works', NULL, 'Parent label', 60), (2, 'Velvet Shorts', 1, NULL, NULL);
""")
con.commit()
PY
fi

# Run the whole journey with encryption of user uploads on.
KEY=frontend/e2e/.uploads-key
[ -f "$KEY" ] || { python3 -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())" > "$KEY"; chmod 400 "$KEY"; }
export UPLOADS_KEY_FILE="$PWD/$KEY"
export BACKEND_TAG=e2e   # its own image tag, so a test build never replaces the real one
compose=(docker compose -p goon-e2e -f compose.yaml -f compose.e2e.yaml)
cleanup() { [ "${1:-}" = "--keep" ] || "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1; }
trap 'cleanup "${1:-}"' EXIT
"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" build goonstreamer-api goonstreamer-worker goonstreamer-web
"${compose[@]}" up -d goonstreamer-db goonstreamer-api goonstreamer-worker goonstreamer-web
"${compose[@]}" run --rm goonstreamer-e2e
