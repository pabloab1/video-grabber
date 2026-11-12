import os
import shutil
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse

from flask import Flask, render_template, request, send_file, jsonify, after_this_request
import yt_dlp

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG_DIR = os.path.join(BASE_DIR, "bin")


def find_ffmpeg():
    """Locate ffmpeg. Returns None to let yt-dlp fall back to PATH."""
    # 1. A binary next to the app: bin/ffmpeg.exe on Windows (local dev), or
    #    bin/ffmpeg downloaded by fetch_ffmpeg.py during a Linux build.
    for cand in ("ffmpeg.exe", "ffmpeg"):
        if os.path.exists(os.path.join(FFMPEG_DIR, cand)):
            return FFMPEG_DIR
    # 2. imageio-ffmpeg ships a static ffmpeg as a wheel, so it survives on any
    #    host even if no build script ran (Render, Railway, Heroku, a plain VPS).
    try:
        import imageio_ffmpeg
        return os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return None  # fall back to ffmpeg on PATH


FFMPEG_LOCATION = find_ffmpeg()

# --- Tunables (env-overridable so the hosted instance can be adjusted) --------
MAX_FILESIZE = int(os.environ.get("MAX_FILESIZE_MB", "500")) * 1024 * 1024
JOB_TTL = 600                       # keep terminal jobs this long for polling
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "4"))
STALL_SECONDS = int(os.environ.get("STALL_SECONDS", "90"))
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SECONDS", "1800"))
FRAG_CONCURRENCY = int(os.environ.get("FRAG_CONCURRENCY", "2"))
PROXY = os.environ.get("YDL_PROXY") or None
COOKIE_FILE = os.environ.get("YDL_COOKIE_FILE") or None

# Browser TLS/HTTP2 fingerprint impersonation. A plain Python client is trivially
# detected even with a spoofed User-Agent; impersonating a real Chrome handshake
# is what gets past most bot checks. Requires the curl_cffi package.
try:
    import curl_cffi  # noqa: F401
    from yt_dlp.networking.impersonate import ImpersonateTarget
    _IMPERSONATE = ImpersonateTarget.from_str("chrome")
except Exception:
    _IMPERSONATE = None

# Disabled at runtime if yt-dlp rejects the target (older/newer curl_cffi).
USE_IMPERSONATE = _IMPERSONATE is not None

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

jobs = {}
jobs_lock = threading.Lock()


class Cancelled(Exception):
    pass


class Stalled(Exception):
    pass


class TimedOut(Exception):
    pass


def valid_url(u: str) -> bool:
    p = urlparse(u)
    return p.scheme in ("http", "https") and bool(p.netloc)


def new_job(url):
    jid = uuid.uuid4().hex
    job = {
        "id": jid,
        "url": url,
        "status": "queued",  # queued | downloading | done | error | cancelled
        "percent": 0.0,
        "speed": "",
        "eta": "",
        "message": None,
        "attempt": 0,
        "filename": None,
        "error": None,
        "path": None,
        "tmp": None,
        "cancel": False,
        "created": time.time(),
    }
    with jobs_lock:
        jobs[jid] = job
    return job


# Files yt-dlp leaves behind that are not the finished media.
TEMP_MARKERS = (".part", ".ytdl", ".temp", ".frag", ".info.json")


def is_temp(name):
    return any(m in name for m in TEMP_MARKERS)


def backoff(attempt):
    """Exponential backoff, capped. attempt is 1-based."""
    return min(2 ** attempt, 20)


def build_opts(job, tmp, attempt):
    opts = {
        "outtmpl": os.path.join(tmp, "%(title).80B.%(ext)s"),
        # First attempts ask for separate best video + audio (needs a merge).
        # Later attempts fall back to a single progressive file, which survives
        # hosts where merging fails.
        "format": "bv*+ba/b" if attempt <= 2 else "b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "max_filesize": MAX_FILESIZE,
        "ffmpeg_location": FFMPEG_LOCATION,

        # --- resilience: the actual fix for downloads dying partway ----------
        "retries": 10,                 # per-request network retries
        "fragment_retries": 10,        # per-fragment retries (HLS/DASH)
        "extractor_retries": 5,
        "file_access_retries": 5,
        "retry_sleep_functions": {
            "http": backoff,
            "fragment": backoff,
            "file_access": backoff,
        },
        "continuedl": True,            # resume .part files instead of restarting
        "skip_unavailable_fragments": True,  # one dead fragment must not kill the job
        # Chunked GETs: a dropped connection costs one chunk, not the whole file.
        "http_chunk_size": 10 * 1024 * 1024,
        "socket_timeout": 30,
        # Fewer parallel connections = far less likely to trip rate limiting.
        "concurrent_fragment_downloads": FRAG_CONCURRENCY,
        "progress_hooks": [make_hook(job)],
    }

    if PROXY:
        opts["proxy"] = PROXY
    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE

    if USE_IMPERSONATE:
        # Impersonation supplies a coherent header set; overriding the UA on top
        # of it would re-create the mismatch we are trying to avoid.
        opts["impersonate"] = _IMPERSONATE
    else:
        opts["http_headers"] = {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
    return opts


def make_hook(job):
    """Progress hook that also aborts an attempt when the transfer goes silent."""

    def hook(d):
        if job["cancel"]:
            raise Cancelled()
        if time.time() - job["started"] > JOB_TIMEOUT:
            raise TimedOut()

        st = d.get("status")
        if st == "downloading":
            job["status"] = "downloading"
            done = d.get("downloaded_bytes")
            if done is not None and done > job.get("last_bytes", -1):
                job["last_bytes"] = done
                job["last_progress"] = time.time()
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total and done is not None:
                job["percent"] = round(done / total * 100, 1)
            job["speed"] = (d.get("_speed_str") or "").strip()
            job["eta"] = (d.get("_eta_str") or "").strip()
            if time.time() - job["last_progress"] > STALL_SECONDS:
                raise Stalled()
        elif st == "finished":
            job["percent"] = 100.0

    return hook


def clean_error(exc):
    """Turn yt-dlp's noisy errors into something a user can act on."""
    msg = str(exc).split("ERROR:")[-1].strip()
    low = msg.lower()
    if "403" in msg or "forbidden" in low:
        return ("The site refused the download (HTTP 403). It is blocking this "
                "server's IP — retry later, set YDL_PROXY, or run the app locally.")
    if "429" in msg or "too many requests" in low:
        return ("The site rate-limited this server (HTTP 429). Wait a few "
                "minutes and try again.")
    if any(s in low for s in ("age-restricted", "age restricted", "confirm your age",
                              "sign in", "log in", "login required", "premium",
                              "members only", "private video")):
        return ("This video needs a login or age check. Provide cookies via "
                "YDL_COOKIE_FILE to download it.")
    if "unsupported url" in low or "not a valid url" in low:
        return "That link is not supported."
    if "unable to download webpage" in low or "unable to extract" in low:
        return "Couldn't read that page — the link may be dead or region-locked."
    return msg[:300] or "Could not download that link."


def finish_success(job, tmp):
    files = [f for f in os.listdir(tmp) if not is_temp(f)]
    if not files:
        job["status"] = "error"
        job["error"] = "No video found at that link."
        return
    files.sort(key=lambda f: os.path.getsize(os.path.join(tmp, f)), reverse=True)
    job["path"] = os.path.join(tmp, files[0])
    job["filename"] = files[0]
    job["percent"] = 100.0
    job["message"] = None
    job["status"] = "done"


def run_download(job):
    global USE_IMPERSONATE

    tmp = tempfile.mkdtemp(prefix="grab_")
    job["tmp"] = tmp
    job["started"] = time.time()
    job["last_progress"] = time.time()
    job["last_bytes"] = -1

    error = None
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if job["cancel"]:
                raise Cancelled()

            job["attempt"] = attempt
            job["status"] = "downloading"
            job["message"] = (None if attempt == 1 else
                              f"Connection dropped — retrying ({attempt}/{MAX_ATTEMPTS})…")
            # Reset the silence clock; resumed bytes count as progress.
            job["last_progress"] = time.time()
            job["last_bytes"] = -1

            try:
                with yt_dlp.YoutubeDL(build_opts(job, tmp, attempt)) as ydl:
                    ydl.extract_info(job["url"], download=True)
                if job["cancel"]:
                    raise Cancelled()
                finish_success(job, tmp)
                return

            except (Cancelled, TimedOut):
                raise

            except Stalled:
                error = ("The download stopped moving — the site is likely "
                         "throttling this server.")
                if attempt >= MAX_ATTEMPTS:
                    raise
                time.sleep(min(3 * attempt, 15))

            except yt_dlp.utils.DownloadError as e:
                if job["cancel"]:
                    raise Cancelled()
                text = str(e).lower()
                # If the impersonation target is unusable on this host, drop it
                # and retry immediately rather than failing the whole job.
                if "impersonate" in text and USE_IMPERSONATE:
                    USE_IMPERSONATE = False
                error = clean_error(e)
                if attempt >= MAX_ATTEMPTS:
                    raise
                time.sleep(min(3 * attempt, 15))

    except Cancelled:
        job["status"] = "cancelled"
        job["message"] = None
    except TimedOut:
        job["status"] = "error"
        job["error"] = f"Gave up after {max(1, JOB_TIMEOUT // 60)} minutes."
    except Stalled:
        job["status"] = "error"
        job["error"] = error or "The download stalled."
    except yt_dlp.utils.DownloadError as e:
        job["status"] = "error"
        job["error"] = error or clean_error(e)
    except Exception:
        job["status"] = "error"
        job["error"] = "Something went wrong. Try again."
    finally:
        job["message"] = None
        if job["status"] in ("error", "cancelled"):
            shutil.rmtree(tmp, ignore_errors=True)
            job["tmp"] = None


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/download")
def download():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not valid_url(url):
        return jsonify(error="Paste a full link starting with http:// or https://"), 400
    job = new_job(url)
    threading.Thread(target=run_download, args=(job,), daemon=True).start()
    return jsonify(job=job["id"]), 202


@app.get("/progress")
def progress():
    jid = request.args.get("job", "")
    with jobs_lock:
        job = jobs.get(jid)
    if not job:
        return jsonify(error="Unknown or expired job."), 404
    return jsonify(
        status=job["status"],
        percent=job["percent"],
        speed=job["speed"],
        eta=job["eta"],
        message=job["message"],
        attempt=job["attempt"],
        filename=job["filename"],
        error=job["error"],
    )


@app.post("/cancel")
def cancel():
    jid = (request.get_json(silent=True) or {}).get("job", "")
    with jobs_lock:
        job = jobs.get(jid)
    if not job:
        return jsonify(error="Unknown or expired job."), 404
    if job["status"] not in ("done", "error", "cancelled"):
        job["cancel"] = True
    return jsonify(status=job["status"])


def rmtree_retry(path, attempts=10, delay=0.5):
    # Windows keeps the file handle open briefly after send_file starts
    # streaming, so an immediate rmtree fails with PermissionError. Retry.
    for _ in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(delay)


@app.get("/file")
def file():
    jid = request.args.get("job", "")
    with jobs_lock:
        job = jobs.get(jid)
    if not job or job["status"] != "done" or not job.get("path") or not os.path.exists(job["path"]):
        return jsonify(error="File is no longer available."), 404

    path = job["path"]
    name = job["filename"]

    @after_this_request
    def cleanup(resp):
        # Runs once the response object exists: send_file already holds the
        # file open, so the first rmtree fails on Windows (PermissionError,
        # swallowed) and the retrying thread removes it after streaming ends.
        # Popping the job here keeps the 404-on-double-fetch behavior.
        with jobs_lock:
            jobs.pop(job["id"], None)
        threading.Thread(target=rmtree_retry, args=(os.path.dirname(path),), daemon=True).start()
        return resp

    return send_file(path, as_attachment=True, download_name=name)


def sweep_jobs():
    while True:
        time.sleep(30)
        now = time.time()
        with jobs_lock:
            for jid, job in list(jobs.items()):
                terminal = job["status"] in ("error", "cancelled")
                # A finished job that nobody fetched still holds a temp dir on
                # disk; expire it so the container doesn't fill up.
                abandoned = (job["status"] == "done"
                             and job.get("tmp")
                             and now - job["created"] > JOB_TTL)
                if (terminal and now - job["created"] > JOB_TTL) or abandoned:
                    if abandoned and job.get("tmp"):
                        threading.Thread(
                            target=rmtree_retry, args=(job["tmp"],), daemon=True
                        ).start()
                    jobs.pop(jid, None)


threading.Thread(target=sweep_jobs, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(debug=debug, host="0.0.0.0", port=port)
