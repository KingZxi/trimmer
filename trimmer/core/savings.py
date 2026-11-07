"""Running total of the disk space Trimmer has saved, kept in stats.json.

"Space saved" only counts jobs that really deleted their originals (or every
finished job, if the user deletes originals themselves and says so). The
"reduction" total is informational: originals minus outputs for every
finished job, deleted or not. Everything is approximate.
"""

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .util import app_data_dir, format_size, read_safely, write_safely

STATS_FILE = "stats.json"
_lock = threading.Lock()


@dataclass
class Savings:
    since: str = field(default_factory=lambda: date.today().isoformat())
    saved_bytes: int = 0        # originals - outputs, for jobs that deleted their originals
    reduction_bytes: int = 0    # originals - outputs, for every finished job
    jobs: int = 0
    clips: int = 0              # source clips processed


def path() -> Path:
    return app_data_dir() / STATS_FILE


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def load() -> Savings:
    """The stored totals; a missing or damaged file starts fresh."""
    s = Savings()
    try:
        data = json.loads(read_safely(path()))
    except (OSError, ValueError):
        try:            # damaged file (a crash mid-write): the previous copy
            data = json.loads(path().with_name(STATS_FILE + ".bak").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return s
    if not isinstance(data, dict):
        return s
    if isinstance(data.get("since"), str):
        try:
            s.since = date.fromisoformat(data["since"]).isoformat()
        except ValueError:
            pass
    for k in ("saved_bytes", "reduction_bytes", "jobs", "clips"):
        v = _int(data.get(k))
        if v is not None and (v >= 0 or k.endswith("_bytes")):
            setattr(s, k, v)
    return s


def _save(s: Savings):
    try:
        write_safely(path(), json.dumps(asdict(s), indent=2))
    except OSError:
        pass


def record(result, count_kept: bool = False) -> Savings:
    """Add one finished job (a JobResult). Failed, cancelled and skipped jobs add nothing."""
    with _lock:
        s = load()
        if result.status not in ("done", "reverted"):
            return s
        diff = result.in_bytes - result.out_bytes
        s.reduction_bytes += diff
        s.jobs += 1
        s.clips += result.source_count
        if result.deleted_bytes:
            s.saved_bytes += result.deleted_bytes - result.out_bytes
        elif count_kept:
            s.saved_bytes += diff
        _save(s)
        return s


def record_late_delete(result, nbytes: int, count_kept: bool = False) -> Savings:
    """An original that `result`'s job kept for a later queued job was removed
    after all. Call before adding nbytes to result.deleted_bytes."""
    with _lock:
        s = load()
        if count_kept or result.status not in ("done", "reverted"):
            return s            # already counted in full by record()
        s.saved_bytes += nbytes - (0 if result.deleted_bytes else result.out_bytes)
        _save(s)
        return s


def set_saved(saved_bytes: int) -> Savings:
    """Set the space saved total by hand, e.g. to count files deleted outside Trimmer."""
    with _lock:
        s = load()
        s.saved_bytes = int(saved_bytes)
        _save(s)
        return s


def set_totals(saved_bytes=None, reduction_bytes=None, clips=None, jobs=None, since=None) -> Savings:
    """Set any of the figures by hand (Settings > Files > Space saved > Edit)."""
    with _lock:
        s = load()
        for k, v in (("saved_bytes", saved_bytes), ("reduction_bytes", reduction_bytes)):
            if v is not None:
                setattr(s, k, int(v))
        for k, v in (("clips", clips), ("jobs", jobs)):
            if v is not None:
                setattr(s, k, max(0, int(v)))
        if since is not None:
            s.since = date.fromisoformat(since).isoformat()
        _save(s)
        return s


def reset() -> Savings:
    with _lock:
        s = Savings()
        _save(s)
        return s


def pretty_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d.day} {d.strftime('%b %Y')}"


def summary_text(s: Savings = None) -> str:
    """One line for the queue footer and the settings page."""
    s = s or load()
    diff = s.reduction_bytes
    how = "smaller" if diff >= 0 else "larger"
    return (f"Space saved: {format_size(s.saved_bytes)}  ·  outputs {format_size(abs(diff))} {how} than their "
            f"sources  ·  {s.clips:,} clip{'' if s.clips == 1 else 's'} since {pretty_date(s.since)}")
