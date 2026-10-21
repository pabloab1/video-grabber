# Video Grabber

Paste a video link, get the file. Flask + yt-dlp.

## Structure

    app.py               Flask server (renders template, streams download)
    templates/index.html Front-end
    requirements.txt     Python deps (incl. imageio-ffmpeg as a fallback ffmpeg)
    fetch_ffmpeg.py      Downloads a static ffmpeg into ./bin at build time
    bin/                 Bundled ffmpeg on Windows (git-ignored; fetched on Linux)

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

ffmpeg is located by `find_ffmpeg()`, in this order: a binary in `bin/`
(bundled on Windows, or fetched by `fetch_ffmpeg.py` on a Linux build),
then the `imageio-ffmpeg` wheel, then anything on `PATH`. So the
video+audio merge works out of the box on any host without apt or Docker.

## Deploy (Render — free, runs as-is, no Docker)

The repo is pre-wired for Render via `render.yaml` + `build.sh`. The build script
installs Python deps and runs `fetch_ffmpeg.py`, which downloads a prebuilt static
ffmpeg into `bin/` (Render's native build image has no working apt, so a static
download is used instead of `apt-get` / Docker). If that download ever fails, the
`imageio-ffmpeg` dependency still provides a working ffmpeg at runtime.

Two routes:

**One-click:** https://render.com/deploy?repo=https://github.com/pabloab1/video-grabber
— authorizes Render with GitHub and creates the service from `render.yaml`. No config
needed.

**Manual:** New → Blueprint → connect the repo → Render reads `render.yaml`.
- Build: `bash build.sh` (deps + static ffmpeg)
- Start: `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 300 app:app`
  (one worker is deliberate — jobs live in process memory, so a second worker would
  split the job table and 404 your progress polls)
- Health check: `GET /`
- Free plan: 512 MB RAM, sleeps after 15 min idle (first hit after sleep is slow).

Also runs on Railway/Heroku (a `Procfile` is included). Bind to `$PORT` and you're done.

## Keep it working

Sites change often. Run `pip install -U yt-dlp` regularly.
