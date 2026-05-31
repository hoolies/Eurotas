#!/usr/bin/env bash
# Eurotas hub setup: install tools and mount the RAM (tmpfs) cache.
# Re-run safely; it is idempotent.
set -euo pipefail

HLS_DIR="${EUROTAS_HLS_DIR:-/tmp/eurotas-hls}"
WORK_DIR="${EUROTAS_WORK_DIR:-/tmp/eurotas-work}"
TMPFS_SIZE="${EUROTAS_TMPFS_SIZE:-2G}"

echo "== Eurotas setup =="

# 1) ffmpeg
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found. Install it with your package manager, e.g.:"
  echo "  Debian/Ubuntu:  sudo apt install ffmpeg"
  echo "  Void Linux:     sudo xbps-install -S ffmpeg"
  echo "  Fedora:         sudo dnf install ffmpeg"
else
  echo "ffmpeg: $(ffmpeg -version | head -n1)"
fi

# 2) Python deps: FastAPI + uvicorn (the web layer) and yt-dlp/streamlink (ingest)
echo "Installing Python dependencies (fastapi, uvicorn, yt-dlp, streamlink)..."
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
python3 -m pip install -r "$SCRIPT_DIR/requirements.txt" || \
  python3 -m pip install --user -r "$SCRIPT_DIR/requirements.txt" || \
  python3 -m pip install --user --break-system-packages -r "$SCRIPT_DIR/requirements.txt"

# 3) tmpfs RAM cache for the rolling HLS buffer (nothing persists to disk)
mkdir -p "$HLS_DIR" "$WORK_DIR"
if mountpoint -q "$HLS_DIR"; then
  echo "tmpfs already mounted at $HLS_DIR"
else
  echo "Mounting tmpfs ($TMPFS_SIZE) at $HLS_DIR (needs sudo)..."
  sudo mount -t tmpfs -o "size=$TMPFS_SIZE,mode=0777" tmpfs "$HLS_DIR" || \
    echo "  (could not mount tmpfs; it will still work on regular disk)"
fi

echo
echo "To make the tmpfs permanent, add to /etc/fstab:"
echo "  tmpfs  $HLS_DIR  tmpfs  size=$TMPFS_SIZE,mode=0777  0  0"
echo
echo "Done. Copy config.example.json -> config.json, set your PIN and the"
echo "Weather Network live URL, then run:  python3 run.py -c config.json"
