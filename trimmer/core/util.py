"""Small helpers shared by the whole engine: formatting, safe file
handling and the per-user data folder."""

import os
import re
import sys
import time
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".flv", ".ts", ".avi", ".webm", ".m4v"}

APP_NAME = "Trimmer"
OLD_APP_NAME = "ClipForge"      # earlier name; its settings folder is moved over once


def app_data_dir() -> Path:
    """Where settings, logs and the job journal live.

    Windows: %APPDATA%\\Trimmer. Elsewhere: ~/.config/trimmer (or
    $XDG_CONFIG_HOME). Not next to the script, so it keeps working when the
    app is packaged and never mixes with an output folder.
    """
    override = os.environ.get("TRIMMER_DATA_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME.lower()
    if not override and not base.exists():
        old = base.with_name(OLD_APP_NAME if base.name == APP_NAME else OLD_APP_NAME.lower())
        if old.is_dir():
            try:
                old.rename(base)
            except OSError:
                pass
    return base


def logs_dir() -> Path:
    return app_data_dir() / "Logs"


def format_size(num_bytes) -> str:
    """Human readable size; negative values keep their sign."""
    if num_bytes is None:
        return "?"
    sign = "-" if num_bytes < 0 else ""
    size = float(abs(num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{sign}{size:.1f} {unit}"
        size /= 1024
    return f"{sign}{size:.1f} PB"


def format_hms(seconds) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_hms_ms(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(total_ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}" if h else f"{m}:{s:02d}.{ms:03d}"


def parse_timecode(text: str) -> float:
    """Parse '75', '1:15', '1:15.5' or '0:01:15.500' into seconds."""
    text = text.strip()
    if not text:
        raise ValueError("empty time")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"bad time: {text}")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    if total < 0:
        raise ValueError("negative time")
    return total


_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """Make a string safe to use as a Windows file name component."""
    cleaned = _ILLEGAL.sub("_", name).strip().rstrip(".")
    return cleaned or "untitled"


def safe_unlink(path: Path, attempts: int = 8, delay: float = 0.25) -> None:
    """Delete a file, retrying briefly while Windows still holds a handle.

    A just-killed ffmpeg can keep its output locked for a moment on Windows,
    which raised PermissionError in the old compressor. Kept from there.
    """
    path = Path(path)
    for attempt in range(attempts):
        try:
            if path.exists():
                path.unlink()
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def unique_path(path: Path) -> Path:
    """path, or 'name (2).ext', 'name (3).ext'... if it already exists."""
    path = Path(path)
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def same_file(a: Path, b: Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve() or (
            Path(a).exists() and Path(b).exists() and os.path.samefile(a, b))
    except OSError:
        return False


def recycle_or_delete(path: Path, permanent: bool = False) -> str:
    """Send a file to the Recycle Bin (default) or delete it outright.

    Returns a short description of what happened. Raises OSError if neither
    worked, so the caller can report it instead of silently keeping/losing it.
    """
    path = Path(path)
    if not permanent:
        try:
            from send2trash import send2trash
        except ImportError:
            raise OSError("send2trash is not installed, so the Recycle Bin can't be used. "
                          "Install it (pip install send2trash) or switch to permanent delete in Settings.")
        send2trash(str(path))
        return "moved to Recycle Bin"
    safe_unlink(path)
    return "deleted"


def find_video_files(paths, recursive: bool = True):
    """Expand a mix of files and folders into a sorted list of video files."""
    found = []
    seen = set()
    for p in paths:
        p = Path(p)
        if p.is_dir():
            it = p.rglob("*") if recursive else p.iterdir()
            for f in it:
                if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
                    key = str(f.resolve())
                    if key not in seen:
                        seen.add(key)
                        found.append(f)
        elif p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
            key = str(p.resolve())
            if key not in seen:
                seen.add(key)
                found.append(p)
    return sorted(found, key=lambda f: str(f).lower())
