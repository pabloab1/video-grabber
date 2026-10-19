# Video Grabber

Paste a video link, get the file. Flask + yt-dlp.

## Structure

    app.py               Flask server (renders template, streams download)
    templates/index.html Front-end
    requirements.txt     Python deps
    bin/                 Bundled static ffmpeg + ffprobe (Windows x64) — no system install needed

## How it works

    POST /download  {url}   -> 202 {job} — starts the download in a background thread
    GET  /progress?job=...   -> JSON {status, percent, speed, eta, filename, error}
    POST /cancel   {job}    -> aborts an in-flight download
    GET  /file?job=...      -> streams the finished file (job + temp dir are then cleaned up)

The UI polls /progress every 700ms and shows a live progress bar with speed and ETA.
A watchdog surfaces an error if nothing moves for 5 minutes. yt-dlp runs with
`concurrent_fragment_downloads: 4` so HLS/DASH fragment streams download in parallel
(single-fragment downloads stall badly on throttled CDNs).

## Run locally

    # make your own venv (the bundled bin/ffmpeg.exe makes merge work on Windows):
    python -m venv venv && venv\Scripts\pip install -r requirements.txt
    venv\Scripts\python app.py        # http://localhost:5000

ffmpeg is already bundled in `bin/` and wired up via `ffmpeg_location` in app.py,
so the video+audio merge works out of the box on Windows. `bin/` is git-ignored —
a fresh clone on a machine without it falls back to system ffmpeg on PATH.

## Deploy (Render — free, runs as-is)

This repo is pre-wired for Render via `render.yaml` + `build.sh` (the build script
installs ffmpeg so yt-dlp can merge streams). Two routes:

**One-click:** https://render.com/deploy?repo=https://github.com/pabloab1/video-grabber
— authorizes Render with GitHub and creates the service from `render.yaml`. No config
needed.

**Manual:** New → Blueprint → connect the repo → Render reads `render.yaml` automatically.
- Build: `bash build.sh` (installs deps + ffmpeg)
- Start: `gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 300 app:app`
- Health check: `GET /`
- Free plan: 512 MB RAM, sleeps after 15 min idle (first hit after sleep is slow).

Needs ffmpeg + a long request timeout. Also runs on Railway/Fly.io/Heroku (a `Procfile`
is included). Bind to `$PORT` and you're done.

## Keep it working

Sites change often. Run `pip install -U yt-dlp` regularly.

## Keep it working

Sites change often. Run `pip install -U yt-dlp` regularly.
