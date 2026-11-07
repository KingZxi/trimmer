"""Run logs, queue persistence and crash logs.

- Run log: one human-readable text file per batch, fully rewritten and
  fsynced before and after every job. A hard crash leaves it frozen on
  "IN PROGRESS -- currently processing: X", which tells you exactly where
  it stopped (kept from the old compressor).
- Queue file: the pending queue as JSON, so an interrupted batch can be
  restored when the app next opens. Written crash-safely (util.write_safely),
  so a crash or power cut mid-save leaves the previous copy, never an empty file.
- Crash log: full traceback, written by the app's exception hook.
"""

import json
import os
import platform
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import List

from .util import app_data_dir, format_size, logs_dir, read_safely, write_safely

QUEUE_FILE = "queue.json"


def _fsync_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


class RunLog:
    def __init__(self, settings_line: str = ""):
        stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        self.path = logs_dir() / f"log ({stamp}).txt"
        self.settings_line = settings_line
        self.entries = []
        self.totals = {"attempted": 0, "done": 0, "reverted": 0, "failed": 0, "skipped": 0,
                       "cancelled": 0, "in_bytes": 0, "out_bytes": 0}

    def record(self, name: str, status: str, text: str, in_bytes: int = 0, out_bytes: int = 0):
        self.entries.append((name, f"{status.upper():<9} {text}"))
        self.totals["attempted"] += 1
        self.totals[status] = self.totals.get(status, 0) + 1
        self.totals["in_bytes"] += in_bytes
        self.totals["out_bytes"] += out_bytes

    def write(self, status_line: str):
        t = self.totals
        saved = t["in_bytes"] - t["out_bytes"]
        pct = (saved / t["in_bytes"] * 100) if t["in_bytes"] else 0.0
        lines = ["Trimmer Run Log", f"Settings: {self.settings_line}", "", status_line, "", "Jobs:"]
        if self.entries:
            w = max(len(n) for n, _ in self.entries)
            lines += [f"  {n.ljust(w)}   {r}" for n, r in self.entries]
        else:
            lines.append("  (none yet)")
        lines += ["", "Totals:",
                  f"  Jobs attempted:  {t['attempted']}",
                  f"  Done:            {t['done']}",
                  f"  Reverted:        {t['reverted']}",
                  f"  Failed:          {t['failed']}",
                  f"  Skipped:         {t['skipped']}",
                  f"  Input size:      {format_size(t['in_bytes'])}",
                  f"  Output size:     {format_size(t['out_bytes'])}",
                  f"  Space saved:     {format_size(saved)} ({pct:.1f}%)"]
        try:
            _fsync_write(self.path, "\n".join(lines) + "\n")
        except OSError:
            pass


# The queue tab and the queue runner (a separate thread) both save the queue.
_queue_lock = threading.Lock()


def _queue_path() -> Path:
    return app_data_dir() / QUEUE_FILE


def save_queue(jobs_as_dicts):
    with _queue_lock:
        try:
            write_safely(_queue_path(), json.dumps(jobs_as_dicts, indent=1))
        except OSError:
            pass


def load_queue():
    """The saved jobs; a file damaged by a crash falls back to the previous copy."""
    p = _queue_path()
    for read in (lambda: read_safely(p), lambda: p.with_name(p.name + ".bak").read_text(encoding="utf-8")):
        try:
            data = json.loads(read())
        except (OSError, json.JSONDecodeError):
            continue
        return data if isinstance(data, list) else []
    return []


def clear_queue():
    """Forget the saved queue, its previous copy too (else load_queue would bring that back)."""
    with _queue_lock:
        p = _queue_path()
        for f in (p, p.with_name(p.name + ".bak")):
            try:
                f.unlink()
            except OSError:
                pass


LOG_PATTERNS = ("log (*).txt", "crash (*).txt", "playback.txt")


def log_files() -> List[Path]:
    """Trimmer's own run logs and crash reports (nothing else in the folder is touched)."""
    folder = logs_dir()
    found = []
    for pattern in LOG_PATTERNS:
        try:
            found += [p for p in folder.glob(pattern) if p.is_file()]
        except OSError:
            pass
    return found


def clean_logs(older_than_days: float = 0) -> int:
    """Delete run logs and crash reports last changed more than older_than_days ago
    (0: all of them). Returns how many were deleted."""
    cutoff = time.time() - older_than_days * 86400
    gone = 0
    for p in log_files():
        try:
            if older_than_days <= 0 or p.stat().st_mtime < cutoff:
                p.unlink()
                gone += 1
        except OSError:
            pass
    return gone


def write_crash_log(exc_type, exc, tb) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    content = (f"Trimmer - Crash Report\nTime: {datetime.now().isoformat(timespec='seconds')}\n"
               f"Python: {platform.python_version()}\nOS: {platform.platform()}\n"
               f"Command line: {' '.join(sys.argv)}\n\nTraceback:\n"
               + "".join(traceback.format_exception(exc_type, exc, tb)))
    for folder in (logs_dir(), Path.cwd()):
        try:
            path = folder / f"crash ({stamp}).txt"
            _fsync_write(path, content)
            return path
        except OSError:
            continue
    return Path("")
