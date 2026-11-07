"""Integrity ("nothing lost") checks: a copied trim starts and ends where it should, an encode
has every frame, and which gaps in the frames were already in the source."""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .. import ffmpeg as ff
from .. import packets as pk
from ..ffmpeg import CancelToken, FFmpegError
from ..render import Span
from ..util import format_hms_ms, safe_unlink


def check_copy_trim(source: Path, out: Path, want_start: float, want_end: float) -> Optional[str]:
    """For a stream-copied single-span trim: the output's packets must be an
    unbroken run of the source's packets covering [want_start, want_end]."""
    src = pk.read_packets(source, "video")
    got = pk.read_packets(out, "video")
    if not src.hashes or not got.hashes:
        return None   # can't check (unusual codec); the duration check still ran
    try:
        i = src.hashes.index(got.hashes[0])
    except ValueError:
        return "output doesn't start on a frame from the source"
    if src.hashes[i:i + len(got)] != got.hashes:
        return pk.compare(src.hashes[i:i + len(got)], got.hashes)
    first_pts = src.pts[i]
    last = max(src.pts[i:i + len(got)])
    frame = 1.0 / 60
    if first_pts > max(want_start, min(src.pts)) + 0.002:
        return f"output starts {first_pts - want_start:.2f}s after the requested start"
    if want_end is not None and last + 2 * frame < min(want_end, max(src.pts)) - 0.05:
        return f"output ends {want_end - last:.2f}s before the requested end"
    return None


def _gap_limit(fps: float) -> float:
    """A stretch with no frames longer than this counts as a gap."""
    return max(0.25, 4.0 / fps) if fps else 0.25


def frame_gaps(path: Path, fps: float, start_time: float = 0.0, cancel: CancelToken = None,
               limit: float = None) -> List[Tuple[float, float]]:
    """(time, length) of every stretch of the file with no frames, times
    counted from start_time (the file's own start)."""
    pts = pk.read_pts(path, cancel)
    limit = _gap_limit(fps) if limit is None else limit
    return [(a - start_time, b - a) for a, b in zip(pts, pts[1:]) if b - a > limit]


# A source window: (file, its info, start, stop), in seconds from the file's
# start, laid end to end in the output in this order.
Window = Tuple[Path, object, float, float]


def _span_windows(spans: List[Span]) -> List[Window]:
    return [(s.path, s.info, s.start, s.stop) for s in spans]


def _piece_windows(pj: pk.PacketJoin) -> List[Window]:
    """The packet join's pieces as windows, each as long as total_duration counts it."""
    out = []
    for p in pj.pieces:
        if p.duration is not None:
            stop = p.duration
        elif p.stop is not None and p.stop < len(p.video):
            stop = p.video.pts[p.stop] - p.clip.info.start_time
        else:
            stop = p.clip.duration
        out.append((p.clip.path, p.clip.info, 0.0, stop))
    return out


@dataclass
class SourceGap:
    at: float               # where it is in the output
    length: float
    source: Path
    source_at: float        # where it is in the source file
    damaged: bool = False   # the source has frames there, but they can't be decoded

    def describe(self) -> str:
        what = "damaged frames that can't be played" if self.damaged else "no frames"
        return (f"Gap already in the source: {self.length:.2f}s with {what} at {format_hms_ms(self.at)} "
                f"({self.source.name} at {format_hms_ms(self.source_at)})")


def decoded_gaps(path: Path, info, start: float, end: float, fps: float,
                 cancel: CancelToken = None) -> List[Tuple[float, float]]:
    """(time, length) of the stretches between start and end (seconds from
    the file's start) where decoding the file gives no picture. A source can
    have every packet and still lose frames: damaged ones (a recording
    glitch) are dropped by the decoder, so they show as a gap in any player
    and in a re-encode, though the packet times are unbroken."""
    t0 = info.start_time + max(0.0, start)
    cmd = [ff.ffmpeg_bin(), "-v", "quiet", "-ss", f"{t0:.3f}", "-t", f"{max(0.5, end - start):.3f}",
           "-copyts", "-i", str(path), "-map", "0:v:0", "-fps_mode", "passthrough",
           "-f", "framemd5", "-"]
    tb, pts = 0.001, []
    for line in ff.run_quiet(cmd, cancel=cancel).stdout.splitlines():
        if line.startswith("#tb 0:"):
            n, d = line.split(":", 1)[1].strip().split("/")
            tb = int(n) / int(d)
        elif line and not line.startswith("#"):
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 3 and parts[0] == "0":
                try:
                    pts.append(int(parts[2]) * tb - info.start_time)
                except ValueError:
                    pass
    pts.sort()
    if not pts:
        return [(start, end - start)]
    limit = _gap_limit(fps) / 2
    edges = [start] + pts + [end]       # nothing decoded at either end of the stretch counts too
    return [(a, b - a) for a, b in zip(edges, edges[1:]) if b - a > limit]


def _decoded_match(sources: List[Window], at: float, length: float, fps: float, slack: float,
                   cancel: CancelToken = None) -> Optional[SourceGap]:
    """The source stretch an output gap at `at` came from, decoded: a SourceGap
    when the source gives no picture there either (damaged frames), else None."""
    offset = 0.0
    for path, info, start, stop in sources:
        span = max(0.0, stop - start)
        if offset - slack <= at < offset + span and info.has_video:
            src_at = start + max(0.0, at - offset)
            lo, hi = max(start, src_at - 1.0), min(stop, src_at + length + 1.0)
            for a, g in decoded_gaps(Path(path), info, lo, hi, fps, cancel):
                if abs(a - src_at) <= 0.5 + slack and length <= g + slack:
                    return SourceGap(at, length, Path(path), a, damaged=True)
            return None
        offset += span
    return None


def source_gaps(windows: List[Window], fps: float, cancel: CancelToken = None) -> List[SourceGap]:
    """The frame gaps the sources already have inside the parts that are
    used, placed on the output's timeline."""
    limit = _gap_limit(fps) / 2         # a bit lower, so a gap just under the limit still explains one
    found, offset = [], 0.0
    for path, info, start, stop in windows:
        if info.has_video:
            for a, g in frame_gaps(path, fps, info.start_time, cancel, limit):
                lo, hi = max(a, start), min(a + g, stop)
                if hi - lo > limit:
                    found.append(SourceGap(offset + lo - start, hi - lo, Path(path), a))
        offset += max(0.0, stop - start)
    return found


def check_frames(out: Path, expected_duration: float, fps: float, sources: List[Window] = None,
                 found: List[SourceGap] = None, cancel: CancelToken = None) -> Optional[str]:
    """For re-encoded outputs: the frame count must match the duration (no
    missing stretch) and timestamps must have no gaps. A gap the sources
    already had at that place (given as windows) is no fault of the output:
    it is added to `found` instead."""
    if not fps:
        return None
    try:
        start = ff.probe(out, use_cache=False).start_time
    except FFmpegError:
        start = 0.0
    gaps = frame_gaps(out, fps, start, cancel)
    if not gaps:
        return None
    known = source_gaps(sources, fps, cancel) if sources else []
    slack = max(0.1, 3.0 / fps)
    for a, g in gaps:
        match = next((k for k in known if abs(a - k.at) <= 0.5 + slack and g <= k.length + slack), None)
        if match is None and sources:
            match = _decoded_match(sources, a, g, fps, slack, cancel)
            if match is not None:
                known.append(match)
        if match is None:
            if sources:
                return f"output has a {g:.2f}s gap in its frames at {format_hms_ms(a)} that isn't in the source"
            return f"output has a {g:.2f}s gap in its frames at {format_hms_ms(a)}"
        known.remove(match)
        if found is not None:
            found.append(SourceGap(a, g, match.source, match.source_at, match.damaged))
    return None


def _report_gaps(ctx, gaps: List[SourceGap]):
    for gap in gaps:
        text = gap.describe()
        if text not in ctx.warnings:        # a hidden build for rips finds the same ones again
            ctx.log(f"{text}. The picture holds still there in the output too, as it did in the source.")
            ctx.warnings.append(text)


def _note_source_gaps(ctx, windows: List[Window], fps: float):
    """For outputs that copy the source frames: report the gaps they carry."""
    try:
        _report_gaps(ctx, source_gaps(windows, fps or 60.0, ctx.cancel))
    except (FFmpegError, OSError, ValueError) as e:
        ctx.log(f"Couldn't check the source for gaps in its frames: {e}")


def _verify_frames(ctx, part: Path, total: float, fps: float, windows: List[Window], what: str):
    """check_frames for an encoded output; a gap from the source is reported, any other fails it."""
    found: List[SourceGap] = []
    problem = check_frames(part, total, fps, windows, found, ctx.cancel)
    if problem:
        safe_unlink(part)
        raise FFmpegError(f"{what} failed verification: {problem}")
    _report_gaps(ctx, found)


def _gap_section(gaps: List[str]) -> str:
    """The source gaps as a section for an info file ("" when there are none)."""
    gaps = list(dict.fromkeys(gaps))
    if not gaps:
        return ""
    lines = ["", "Gaps already in the source", "--------------------------",
             "The source footage has no frames for these stretches, or only damaged ones that can't be",
             "played (it was recorded that way), so the picture holds still there while the sound plays",
             "on. The output keeps them as they are.", ""]
    return "\n".join(lines + [f"  {g.split(': ', 1)[1]}" for g in gaps]) + "\n"
