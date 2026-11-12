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
    GET  /progress?job=...   -> JSON {status, percent, speed, eta, message, attempt, filename, error}
    POST /cancel   {job}    -> aborts an in-flight download
    GET  /file?job=...      -> streams the finished file (job + temp dir are then cleaned up)

The UI polls /progress every 700ms and shows a live progress bar with speed and ETA.

### Surviving mid-download blocks

A download used to die the moment a connection dropped. Now each job:

- retries every request and fragment (10× each) with capped exponential backoff;
- resumes from the existing `.part` file instead of restarting from zero;
- keeps partial files between attempts (they are deleted only after every
  attempt has failed);
- skips unavailable fragments, so one dead segment can't kill the whole video;
- uses chunked HTTP GETs (10 MB), so a dropped connection costs one chunk;
- aborts and resumes an attempt that makes no byte progress for `STALL_SECONDS`;
- impersonates a real Chrome TLS/HTTP2 fingerprint (curl_cffi), which is what
  gets past bot checks that reject plain Python clients;
- falls back from `bv*+ba/b` (separate streams + merge) to a single progressive
  file on later attempts.

`attempt` and `message` in the progress payload drive the "retrying (2/4)" line
in the UI.

### Environment variables

| Var | Default | Purpose |
| --- | --- | --- |
| `MAX_ATTEMPTS` | 4 | Full download attempts before giving up |
| `STALL_SECONDS` | 90 | Abort + resume an attempt after this long with no bytes |
| `JOB_TIMEOUT_SECONDS` | 1800 | Hard cap per job |
| `FRAG_CONCURRENCY` | 2 | Parallel HLS/DASH fragments (higher is more likely to be rate-limited) |
| `MAX_FILESIZE_MB` | 500 | Reject anything larger |
| `YDL_PROXY` | – | Route downloads through a proxy, e.g. `http://user:pass@host:port` |
| `YDL_COOKIE_FILE` | – | Netscape cookie file for sites that need a login / age check |

Note: a cloud host's IP is often blocked outright by some sites. That is an IP
reputation issue, not a code one — `YDL_PROXY` or running locally is the fix.

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
