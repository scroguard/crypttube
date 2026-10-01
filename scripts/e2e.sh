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

compose=(docker compose -p goon-e2e -f compose.yaml -f compose.e2e.yaml)
cleanup() { [ "${1:-}" = "--keep" ] || "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1; }
trap 'cleanup "${1:-}"' EXIT
"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" build goonstreamer-api goonstreamer-worker goonstreamer-web
"${compose[@]}" up -d goonstreamer-db goonstreamer-api goonstreamer-worker goonstreamer-web
"${compose[@]}" run --rm goonstreamer-e2e
