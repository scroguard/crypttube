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

compose=(docker compose -p goon-e2e -f compose.yaml -f compose.e2e.yaml)
cleanup() { [ "${1:-}" = "--keep" ] || "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1; }
trap 'cleanup "${1:-}"' EXIT
"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" build goonstreamer-api goonstreamer-worker goonstreamer-web
"${compose[@]}" up -d goonstreamer-db goonstreamer-api goonstreamer-worker goonstreamer-web
"${compose[@]}" run --rm goonstreamer-e2e
