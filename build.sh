#!/usr/bin/env bash
set -euo pipefail

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# ffmpeg is NOT bundled on the Linux host (bin/ffmpeg.exe is Windows-only and
# git-ignored). Install it so yt-dlp can merge separate video+audio streams
# (the bv*+ba format selector requires it).
apt-get update
apt-get install -y ffmpeg
ffmpeg -version | head -1
