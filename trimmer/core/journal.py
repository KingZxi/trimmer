"""Run logs, the resume record and queue persistence.

- Run log: one human-readable text file per batch, fully rewritten and
  fsynced before and after every job. A hard crash leaves it frozen on
  "IN PROGRESS -- currently processing: X", which tells you exactly where
  it stopped (kept from the old compressor).
- Completed record: CSV of finished jobs keyed by each source's path, size
  and modified time, so a replaced file with the same name is redone.
- Queue file: the pending queue as JSON, so an interrupted batch can be
  restored when the app next opens.
- Crash log: full traceback, written by the app's exception hook.
"""

import csv
import json
import os
import platform
import sys
import traceback
from datetime import datetime
from pathlib import Path

from .util import app_data_dir, format_size, logs_dir

COMPLETED_FILE = "completed.csv"
QUEUE_FILE = "queue.json"


def _fsync_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def source_key(path) -> str:
    p = Path(path)
    try:
        st = p.stat()
        return f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return str(p)


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


class CompletedRecord:
    def __init__(self):
        self.path = app_data_dir() / COMPLETED_FILE
        self.keys = set()
        try:
            with open(self.path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("job_key"):
                        self.keys.add(row["job_key"])
        except (OSError, csv.Error):
            pass

    def add(self, job_key: str, outputs):
        self.keys.add(job_key)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.exists() or self.path.stat().st_size == 0
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["timestamp", "job_key", "outputs"])
                w.writerow([datetime.now().isoformat(timespec="seconds"), job_key,
                            " | ".join(str(o) for o in outputs)])
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass


def save_queue(jobs_as_dicts):
    try:
        _fsync_write(app_data_dir() / QUEUE_FILE, json.dumps(jobs_as_dicts, indent=1))
    except OSError:
        pass


def load_queue():
    try:
        data = json.loads((app_data_dir() / QUEUE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def clear_queue():
    try:
        (app_data_dir() / QUEUE_FILE).unlink()
    except OSError:
        pass


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
