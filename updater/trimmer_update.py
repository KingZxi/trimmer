"""Trimmer's update check: the only code in Trimmer that uses the internet.

It does two things, and only when the user has said yes to update checks:
  1. Ask GitHub for the latest published Trimmer release (version, notes, file links).
  2. Download that release's installer and SHA256SUMS.txt, when the user presses Install update.

Nothing is uploaded and nothing about the user, their clips or their settings is sent: the only
requests are plain downloads from the fixed addresses below, the same as opening the Releases page
in a browser. This file deliberately imports nothing from the rest of Trimmer, and the tests check
that no other file in Trimmer uses the network.

The installer can leave this file out ("Local features only"). Trimmer then has no code that
connects to anything, and Settings > Updates says so.
"""

import json
import urllib.request

REPO = "KingZxi/trimmer"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
USER_AGENT = "Trimmer-update-check"     # GitHub requires one; it carries no version or user details
MAX_JSON = 2 * 1024 * 1024
MAX_FILE = 1024 * 1024 * 1024


def _open(url, timeout):
    if not (url == API_LATEST or url.startswith(DOWNLOAD_PREFIX)):
        raise ValueError(f"not a Trimmer release address: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def latest_release(timeout=8.0) -> dict:
    """The latest published release, as {"tag", "name", "notes", "page", "assets": {name: url}}.
    Drafts and pre-releases are never returned by this GitHub address."""
    with _open(API_LATEST, timeout) as r:
        data = json.loads(r.read(MAX_JSON).decode("utf-8"))
    assets = {}
    for a in data.get("assets") or []:
        url = a.get("browser_download_url") or ""
        if isinstance(a.get("name"), str) and url.startswith(DOWNLOAD_PREFIX):
            assets[a["name"]] = url
    page = data.get("html_url") or RELEASES_PAGE
    if not str(page).startswith(f"https://github.com/{REPO}/"):
        page = RELEASES_PAGE
    return {"tag": str(data.get("tag_name") or ""), "name": str(data.get("name") or ""),
            "notes": str(data.get("body") or ""), "page": page, "assets": assets}


def download(url, dest, progress=None, cancelled=None, timeout=30.0):
    """Save a release file to the path `dest`. progress(done_bytes, total_bytes or 0) is called as it
    goes; cancelled() returning True stops it (the partial file is removed)."""
    import os
    done = 0
    try:
        with _open(url, timeout) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            while True:
                if cancelled is not None and cancelled():
                    raise InterruptedError("download cancelled")
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_FILE:
                    raise ValueError("download is far larger than a Trimmer installer")
                f.write(chunk)
                if progress is not None:
                    progress(done, total)
    except BaseException:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    return dest
