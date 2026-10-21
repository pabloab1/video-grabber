"""Fetch a static ffmpeg build into ./bin — no apt, no Docker.

Render's native Python build image has no usable apt (the first deploy died with
"Exited with status 100"), and we deliberately avoid Docker, so instead of
installing ffmpeg we download a prebuilt static binary next to the app.
app.py's find_ffmpeg() picks up ./bin/ffmpeg automatically.

Standard library only, so it runs on whatever minimal build image we land on.
"""
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(BASE_DIR, "bin")

# First reachable URL wins. Both ship statically linked binaries that need no
# shared libraries beyond libc.
LINUX_BUILDS = (
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-linux64-gpl.tar.xz",
)


def have_ffmpeg():
    """Return the path to an already-installed binary, or None."""
    for name in ("ffmpeg", "ffmpeg.exe"):
        path = os.path.join(BIN_DIR, name)
        if os.path.exists(path):
            return path
    return None


def safe_extract(tar, dest):
    """Extract while refusing members that escape `dest` (path traversal)."""
    dest = os.path.realpath(dest)
    for member in tar.getmembers():
        target = os.path.realpath(os.path.join(dest, member.name))
        if target != dest and not target.startswith(dest + os.sep):
            continue
        tar.extract(member, dest)


def download(url, archive):
    print("downloading", url, flush=True)
    with urllib.request.urlopen(url, timeout=300) as resp, open(archive, "wb") as out:
        shutil.copyfileobj(resp, out)


def main():
    found = have_ffmpeg()
    if found:
        print("ffmpeg already present:", found)
        return 0

    if sys.platform.startswith("win"):
        # Local Windows dev already ships bin/ffmpeg.exe; nothing to fetch.
        print("Windows host: skipping (use the bundled bin/ffmpeg.exe).")
        return 0

    os.makedirs(BIN_DIR, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="ffmpeg-fetch-")
    try:
        archive = os.path.join(workdir, "ffmpeg.tar.xz")
        for url in LINUX_BUILDS:
            try:
                download(url, archive)
                with tarfile.open(archive) as tar:
                    safe_extract(tar, workdir)
            except Exception as exc:
                print("failed to fetch", url, "->", exc, flush=True)
                continue

            copied = []
            for root, _dirs, files in os.walk(workdir):
                for name in files:
                    if name in ("ffmpeg", "ffprobe"):
                        src = os.path.join(root, name)
                        dst = os.path.join(BIN_DIR, name)
                        shutil.copyfile(src, dst)
                        mode = os.stat(dst).st_mode
                        os.chmod(dst, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                        copied.append(dst)
            if copied:
                print("installed:", ", ".join(copied))
                break
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    found = have_ffmpeg()
    print("ffmpeg ->", found)
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
