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

MAX_FILESIZE = 500 * 1024 * 1024  # 500 MB cap
JOB_TTL = 600  # seconds a terminal (error/cancelled) job is kept for polling

# A realistic browser UA helps with sites that reject non-browser clients.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

jobs = {}
jobs_lock = threading.Lock()


class Cancelled(Exception):
    pass


def valid_url(u: str) -> bool:
    p = urlparse(u)
    return p.scheme in ("http", "https") and bool(p.netloc)


def new_job(url):
    jid = uuid.uuid4().hex
    job = {
        "id": jid,
        "url": url,
        "status": "queued",  # queued | downloading | merging | done | error | cancelled
        "percent": 0.0,
        "speed": "",
        "eta": "",
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


def run_download(job):
    tmp = tempfile.mkdtemp(prefix="grab_")
    job["tmp"] = tmp

    def hook(d):
        if job["cancel"]:
            raise Cancelled()
        st = d.get("status")
        if st == "downloading":
            job["status"] = "downloading"
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes")
            if total and done is not None:
                job["percent"] = round(done / total * 100, 1)
            job["speed"] = (d.get("_speed_str") or "").strip()
            job["eta"] = (d.get("_eta_str") or "").strip()
        elif st == "finished":
            job["percent"] = 100.0

    opts = {
        "outtmpl": os.path.join(tmp, "%(title).80B.%(ext)s"),
        "format": "bv*+ba/b",          # best video + audio, fall back to best single file
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,            # stdout stays quiet; progress goes to the UI via the hook
        "max_filesize": MAX_FILESIZE,
        "ffmpeg_location": FFMPEG_LOCATION,  # bundled on Windows; system ffmpeg in prod
        "concurrent_fragment_downloads": 4,  # parallel HLS/DASH fragments; single-stream CDNs crawl otherwise
        "http_headers": {"User-Agent": USER_AGENT},
        "progress_hooks": [hook],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(job["url"], download=True)
        if job["cancel"]:
            raise Cancelled()
        files = [f for f in os.listdir(tmp) if not f.endswith(".part")]
        if not files:
            job["status"] = "error"
            job["error"] = "No video found at that link."
            return
        files.sort(key=lambda f: os.path.getsize(os.path.join(tmp, f)), reverse=True)
        name = files[0]
        job["path"] = os.path.join(tmp, name)
        job["filename"] = name
        job["percent"] = 100.0
        job["status"] = "done"
    except Cancelled:
        job["status"] = "cancelled"
    except yt_dlp.utils.DownloadError as e:
        if job["cancel"]:
            job["status"] = "cancelled"
        else:
            msg = str(e).split("ERROR:")[-1].strip()
            job["status"] = "error"
            job["error"] = msg[:300] or "Could not download that link."
    except Exception:
        job["status"] = "error"
        job["error"] = "Something went wrong. Try again."
    finally:
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
                if job["status"] in ("error", "cancelled") and now - job["created"] > JOB_TTL:
                    jobs.pop(jid, None)


threading.Thread(target=sweep_jobs, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(debug=debug, host="0.0.0.0", port=port)
