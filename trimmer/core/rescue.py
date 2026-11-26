"""Rescuing a damaged recording: find every stretch of it that can still be read, and join those
stretches into a new file, leaving out only the damaged part.

A recording damaged partway (a bad sector, a crash while the file was being written) often still
plays past the damage, because a player jumps over what it can't read. ffmpeg doesn't: it stops at
the first unreadable spot, so a trim or compression of the file comes out short. Rescue reads the
file's packets (fast: nothing is decoded), and where reading stops before the end it jumps further
and further ahead until reading works again, then narrows down where the good part starts."""

import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import ffmpeg as ff
from .ffmpeg import Cancelled, CancelToken, FFmpegError

Stretch = Tuple[float, float]       # (first, last) packet time that could be read, in seconds

TEST_SECONDS = 3.0          # how much must read cleanly after a jump for it to count as readable
MIN_READABLE = 1.0          # ... of which at least this much
CUT_MARGIN = 0.5            # stop this far before the last readable packet: right at it is the damage
REFINE_STEPS = 6            # halvings when narrowing down where the readable part starts again


def _end_slack(duration: float) -> float:
    """How far before the end reading may stop and still count as the whole file."""
    return max(1.5, duration * 0.002)


def _stream(info: ff.MediaInfo) -> str:
    return "v:0" if info.has_video else "a:0"


def _read_span(src: Path, stream: str, interval: Optional[str], cancel: CancelToken = None,
               on_time: Callable[[float], None] = None) -> Optional[Stretch]:
    """(first, last) packet time of `stream` that ffprobe can read in `interval` (a -read_intervals
    entry; None: the whole file), or None when nothing could be read. Reads packets only."""
    cmd = [ff.ffprobe_bin(), "-v", "error", "-select_streams", stream]
    if interval:
        cmd += ["-read_intervals", interval]
    cmd += ["-show_entries", "packet=pts_time", "-of", "csv=p=0", str(src)]
    try:
        process = ff._spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise FFmpegError("ffprobe was not found. Install ffmpeg or set its folder in Settings.")
    if cancel is not None:
        cancel.register(process)
    first = last = None
    n = 0
    try:
        for line in process.stdout:
            try:
                t = float(line.strip().split(",")[0])
            except ValueError:
                continue                    # N/A
            first = t if first is None else min(first, t)
            last = t if last is None else max(last, t)
            n += 1
            if n % 2000 == 0:
                if cancel is not None and cancel.cancelled:
                    raise Cancelled()
                if on_time is not None:
                    on_time(last)
        process.wait()
    finally:
        if cancel is not None:
            cancel.unregister(process)
        if process.poll() is None:
            ff._kill(process)
            process.wait()
        ff._forget(process)
    if cancel is not None and cancel.cancelled:
        raise Cancelled()
    return None if first is None else (first, last)


def _readable_after(src: Path, stream: str, t: float, after: float, cancel) -> Optional[float]:
    """The first packet time when reading from `t` works again past `after`, else None."""
    got = _read_span(src, stream, f"{t:.3f}%+{TEST_SECONDS:g}", cancel)
    if got is None or got[0] <= after or got[1] - got[0] < MIN_READABLE:
        return None
    return got[0]


def readable_stretches(src: Path, info: ff.MediaInfo, cancel: CancelToken = None,
                       on_step: Callable[[str, Optional[float]], None] = None) -> List[Stretch]:
    """Every stretch of the file that can be read, in order. One stretch reaching the end: nothing
    is damaged. One that stops early: nothing after the damage could be read (the file ends there)."""
    src = Path(src)
    stream = _stream(info)
    duration = info.duration or 0.0
    say = on_step or (lambda text, fraction: None)

    def scan(interval, start):
        say("Reading the recording", start / duration if duration else None)
        return _read_span(src, stream, interval, cancel,
                          lambda t: say("Reading the recording", t / duration if duration else None))

    first = scan(None, 0.0)
    if first is None:
        raise FFmpegError(f"Nothing in {src.name} can be read.")
    stretches = [first]
    while duration and stretches[-1][1] < duration - _end_slack(duration):
        stop = stretches[-1][1]
        say(f"Looking past the damage at {_hms(stop)}", stop / duration)
        # Jump ahead 1, 2, 4, 8... seconds until reading works again.
        step, good, good_at = 1.0, None, None
        while stop + step < duration:
            at = _readable_after(src, stream, stop + step, stop, cancel)
            if at is not None:
                good, good_at = stop + step, at
                break
            step *= 2
        if good is None:
            break                       # nothing after this point can be read
        # Narrow it down: the readable part may start well before the jump that found it.
        bad = stop
        for _ in range(REFINE_STEPS):
            if good - bad < 0.5:
                break
            mid = (bad + good) / 2
            at = _readable_after(src, stream, mid, stop, cancel)
            if at is not None:
                good, good_at = mid, min(good_at, at)
            else:
                bad = mid
        nxt = scan(f"{good_at:.3f}%", good_at)
        if nxt is None or nxt[1] <= stop:
            break
        stretches.append((max(nxt[0], good_at), nxt[1]))
    return stretches


def is_damaged(stretches: List[Stretch], duration: float) -> bool:
    return len(stretches) > 1 or (bool(duration) and stretches[-1][1] < duration - _end_slack(duration))


def kept_pieces(stretches: List[Stretch], duration: float) -> List[Tuple[float, Optional[float]]]:
    """(in, out) of each stretch as it is copied: each one but the last stops CUT_MARGIN before the
    damage after it (out None: to the end of the file)."""
    out = []
    for i, (a, b) in enumerate(stretches):
        start = 0.0 if i == 0 else a
        reaches_end = i == len(stretches) - 1 and b >= duration - _end_slack(duration)
        stop = None if reaches_end else max(start + 0.1, b - CUT_MARGIN)
        out.append((start, stop))
    return out


def lost_parts(stretches: List[Stretch], duration: float) -> List[Tuple[float, float]]:
    """The parts of the original that couldn't be read: between stretches, and after the last."""
    pieces = kept_pieces(stretches, duration)
    lost = []
    for (_, b), (c, _) in zip(pieces, pieces[1:]):
        lost.append((b, c))
    if pieces[-1][1] is not None:
        lost.append((pieces[-1][1], duration))
    return lost


def map_time(t: float, pieces, duration: float) -> Optional[float]:
    """Where time `t` of the original lands in the rescued file, or None when it was lost."""
    offset = 0.0
    for a, b in pieces:
        end = duration if b is None else b
        if t < a:
            return None
        if t <= end:
            return offset + t - a
        offset += end - a
    return None


def map_segments(segments, pieces, duration: float) -> List[Tuple[float, Optional[float]]]:
    """A job's kept (start, end) ranges of the original, as ranges of the rescued file. A range
    that runs into a lost part is split around it; one that was lost completely is dropped."""
    out = []
    for s, e in segments:
        e = duration if e is None else min(e, duration)
        for a, b in pieces:
            b2 = duration if b is None else b
            lo, hi = max(s, a), min(e, b2)
            if hi - lo > 0.05:
                start = map_time(lo, pieces, duration)
                end = map_time(hi, pieces, duration)
                end = None if (b is None and hi >= duration - 1e-6) else end
                if out and out[-1][1] is not None and abs(out[-1][1] - start) < 0.01:
                    start = out.pop()[0]          # one range split only by the join: keep it whole
                out.append((start, end))
    return out


def write_rescued(src: Path, pieces, duration: float, out: Path, cancel: CancelToken = None,
                  on_progress: ff.ProgressCallback = None) -> ff.RunResult:
    """Copy the readable pieces of `src`, in order and untouched (no re-encode), into `out`."""
    total = sum((duration if b is None else b) - a for a, b in pieces)
    path = str(Path(src).resolve()).replace("'", "'\\''")
    lines = []
    for a, b in pieces:
        lines.append(f"file '{path}'")
        if a > 0:
            lines.append(f"inpoint {a:.3f}")
        if b is not None:
            lines.append(f"outpoint {b:.3f}")
    with tempfile.TemporaryDirectory(prefix="trimmer_rescue_") as td:
        listing = Path(td) / "pieces.txt"
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ff.run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-map", "0", "-c", "copy",
                              "-y", str(out)],
                             total or 1.0, on_progress, cancel, output_path=Path(out))


def _hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"
