#!/usr/bin/env bash
set -euo pipefail

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# ffmpeg, without apt and without Docker: download a prebuilt static Linux
# binary into ./bin (git-ignored). Render's native build image has no working
# apt, which is what killed the first deploy ("Exited with status 100").
#
# This step is best-effort on purpose: if the download fails, the app still
# boots, because imageio-ffmpeg (in requirements.txt) ships a ffmpeg binary
# that app.py falls back to automatically.
python fetch_ffmpeg.py || echo "warning: static ffmpeg fetch failed; falling back to imageio-ffmpeg"
