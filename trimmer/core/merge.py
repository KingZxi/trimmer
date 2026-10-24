"""OBS Replay Buffer session detection and overlap trimming.

Ported from obs_merge.py. The algorithms are unchanged in behaviour:

- clips are grouped into sessions by time continuity (max_gap),
- interior clips whose footage is fully covered by their neighbours are
  dropped, judged with a confident margin floored at 0.25 s,
- each join's overlap is estimated from timestamps, checked against the
  audio, then shortened by a safety margin (widened to 0.5 s when the
  estimate is unverified) and the optional extra padding,
- lossless cuts snap BACKWARDS to a keyframe so footage is repeated,
  never lost.

What changed: prints became log callbacks; audio cross-correlation uses
numpy (same maths, roughly 100x faster); any video extension and a few
filename styles are accepted, with the file's modified time as a fallback.
"""

import array
import datetime as dt
import math
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import ffmpeg as ff
from .ffmpeg import CancelToken, MediaInfo
from .util import format_hms_ms

try:
    import numpy as np
except ImportError:          # pragma: no cover - numpy is in requirements
    np = None

LogFn = Callable[[str], None]

# "Replay 2026-12-07 03-53-47 (2).mp4", "Replay_2026-12-07_03-53-47.mkv",
# or any name containing "YYYY-MM-DD HH-MM-SS" (OBS's default formatting).
_TIMESTAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ _T](\d{2})[-.:](\d{2})[-.:](\d{2})")


def parse_name_timestamp(name: str) -> Optional[dt.datetime]:
    m = _TIMESTAMP_RE.search(name)
    if not m:
        return None
    try:
        return dt.datetime.strptime(f"{m.group(1)} {m.group(2)}-{m.group(3)}-{m.group(4)}",
                                    "%Y-%m-%d %H-%M-%S")
    except ValueError:
        return None


@dataclass
class MergeOptions:
    max_gap: float = 1.5
    safety_margin: float = 0.25          # 0 = off
    extra_padding: float = 0.0
    audio_align: bool = True
    align_margin: float = 3.0
    never_lose: bool = True              # False = old --seamless keyframe behaviour
    lossless_max_repeat: float = 2.0     # max seconds a lossless keyframe cut may repeat
    timestamp_is_end: bool = False       # filename time = save time (clip end)
    use_mtime_fallback: bool = True


class ReplayClip:
    def __init__(self, path: Path, start: dt.datetime, info: MediaInfo, source: str):
        self.path = Path(path)
        self.start = start
        self.info = info
        self.time_source = source        # "filename" or "modified time"

    @property
    def duration(self) -> float:
        return self.info.duration

    @property
    def audio_tracks(self):
        return self.info.audio

    @property
    def has_audio(self) -> bool:
        return bool(self.info.audio)

    @property
    def end(self) -> dt.datetime:
        return self.start + dt.timedelta(seconds=self.duration)

    def __repr__(self):
        return f"<ReplayClip {self.path.name} start={self.start} dur={self.duration:.2f}s>"


def load_clips(paths, opts: MergeOptions, log: LogFn = None,
               cancel: CancelToken = None) -> Tuple[List[ReplayClip], List[Path]]:
    """Probe each file and work out its real-world start time.
    Returns (clips sorted by start, skipped paths)."""
    log = log or (lambda m: None)
    clips, skipped = [], []
    for p in paths:
        if cancel:
            cancel.check()
        p = Path(p)
        try:
            info = ff.probe(p)
        except ff.FFmpegError as e:
            log(f"Skipping {p.name}: {e}")
            skipped.append(p)
            continue
        if not info.has_video or info.duration <= 0:
            log(f"Skipping {p.name}: no video stream")
            skipped.append(p)
            continue
        ts = parse_name_timestamp(p.name)
        source = "filename"
        if ts is None:
            if not opts.use_mtime_fallback:
                log(f"Skipping {p.name}: no 'YYYY-MM-DD HH-MM-SS' time in its name")
                skipped.append(p)
                continue
            # The file is finished being written when the replay is saved,
            # so its modified time is (close to) the clip's end.
            end = dt.datetime.fromtimestamp(p.stat().st_mtime)
            ts = end - dt.timedelta(seconds=info.duration)
            source = "modified time"
        elif opts.timestamp_is_end:
            ts = ts - dt.timedelta(seconds=info.duration)
        clips.append(ReplayClip(p, ts, info, source))
    clips.sort(key=lambda c: c.start)
    return clips, skipped


def group_into_sessions(clips: List[ReplayClip], max_gap: float, log: LogFn = None):
    """Group sorted clips into sessions based on time continuity."""
    log = log or (lambda m: None)
    if not clips:
        return []
    sessions = [[clips[0]]]
    session_end = clips[0].end
    for clip in clips[1:]:
        gap = (clip.start - session_end).total_seconds()
        if gap > max_gap:
            log(f"Gap of {gap:.1f}s before {clip.path.name} (previous session ended "
                f"{session_end.strftime('%H:%M:%S')}, this clip starts "
                f"{clip.start.strftime('%H:%M:%S')}): starting a new session.")
            sessions.append([clip])
        else:
            sessions[-1].append(clip)
        session_end = max(session_end, clip.end)
    return sessions


def is_fully_covered(prev, nxt, clip, gap_tolerance, margin=0.25) -> bool:
    """True if `clip`'s whole time range is already inside `prev` and/or
    `nxt`. Every boundary needs `margin` seconds of confident headroom:
    being wrong in the optimistic direction would silently delete footage,
    being wrong in the conservative direction just keeps a redundant clip."""
    gap_td = dt.timedelta(seconds=gap_tolerance)
    margin_td = dt.timedelta(seconds=margin)
    if prev.end >= clip.end + margin_td:
        return True
    if nxt.start <= clip.start - margin_td and nxt.end >= clip.end + margin_td:
        return True
    if (prev.end >= clip.start + margin_td
            and nxt.start <= prev.end + gap_td
            and nxt.end >= clip.end + margin_td):
        return True
    return False


def prune_redundant_clips(session, gap_tolerance, margin=0.25):
    """Drop interior clips fully duplicated by their neighbours. The first
    and last clip are never dropped. Returns (kept, dropped)."""
    if len(session) <= 2:
        return list(session), []
    kept, dropped = [session[0]], []
    kept_last = session[0]
    for i in range(1, len(session) - 1):
        clip, nxt = session[i], session[i + 1]
        if is_fully_covered(kept_last, nxt, clip, gap_tolerance, margin=margin):
            dropped.append(clip)
            continue
        kept.append(clip)
        kept_last = clip
    kept.append(session[-1])
    return kept, dropped


# --------------------------------------------------------------------------
# Audio alignment
# --------------------------------------------------------------------------

def extract_audio_samples(path: Path, start: float, duration: float, sample_rate: int,
                          cancel: CancelToken = None):
    """Decode mono 16-bit PCM from audio track 1. Returns a numpy int array
    (or array('h') without numpy), or None if nothing usable came out."""
    if start < 0:
        duration += start
        start = 0.0
    if duration <= 0:
        return None
    if cancel:
        cancel.check()
    cmd = [ff.ffmpeg_bin(), "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
           "-i", str(path), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(sample_rate),
           "-f", "s16le", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, creationflags=ff._NO_WINDOW)
    except FileNotFoundError:
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    raw = out.stdout[: len(out.stdout) // 2 * 2]
    if np is not None:
        return np.frombuffer(raw, dtype="<i2").astype(np.float64)
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def best_alignment(ref, window, step: int = 1):
    """Slide `ref` across `window`; return (best_index, cosine_similarity)."""
    n = len(ref)
    if n == 0 or len(window) < n:
        return None, None
    if np is not None:
        ref = np.asarray(ref, dtype=np.float64)
        win = np.asarray(window, dtype=np.float64)
        e_ref = float(np.dot(ref, ref))
        if e_ref == 0:
            return None, None
        # FFT cross-correlation for every lag, then normalise by each
        # window position's energy (cumulative sum of squares).
        size = 1 << int(math.ceil(math.log2(len(win) + n)))
        corr = np.fft.irfft(np.fft.rfft(win, size) * np.conj(np.fft.rfft(ref, size)), size)
        corr = corr[: len(win) - n + 1]
        csum = np.concatenate(([0.0], np.cumsum(win * win)))
        e_win = csum[n:] - csum[:-n]
        with np.errstate(divide="ignore", invalid="ignore"):
            score = np.where(e_win > 0, corr / np.sqrt(e_ref * np.maximum(e_win, 1e-12)), 0.0)
        score = score[::step]
        idx = int(np.argmax(score))
        return idx * step, float(score[idx])
    # Pure-Python fallback (the old implementation, ~10 ms steps).
    best, best_idx = -2.0, None
    sa = sum(x * x for x in ref)
    if sa == 0:
        return None, None
    for idx in range(0, len(window) - n + 1, max(step, 80)):
        seg = window[idx:idx + n]
        sb = sum(y * y for y in seg)
        if sb == 0:
            continue
        s = sum(x * y for x, y in zip(ref, seg)) / math.sqrt(sa * sb)
        if s > best:
            best, best_idx = s, idx
    return best_idx, (best if best_idx is not None else None)


def refine_overlap_with_audio(prev_clip, cur_clip, naive_overlap: float, sample_rate: int = 8000,
                              ref_len: float = 1.0, margin: float = 3.0, cancel=None):
    """Cross-correlate the last `ref_len` s of prev_clip's audio against a
    window of cur_clip centred on the timestamp estimate. Returns
    (refined_overlap, confidence) or (naive_overlap, None)."""
    if naive_overlap <= 0 or not (prev_clip.has_audio and cur_clip.has_audio):
        return naive_overlap, None
    ref_len = min(ref_len, prev_clip.duration, cur_clip.duration)
    if ref_len < 0.2:
        return naive_overlap, None
    ref_start = max(0.0, prev_clip.duration - ref_len)
    ref = extract_audio_samples(prev_clip.path, ref_start, ref_len, sample_rate, cancel)
    if ref is None or len(ref) < int(sample_rate * ref_len * 0.5):
        return naive_overlap, None
    search_start = max(0.0, naive_overlap - margin)
    search_end = min(cur_clip.duration - ref_len, naive_overlap + margin)
    if search_end <= search_start:
        return naive_overlap, None
    window = extract_audio_samples(cur_clip.path, search_start,
                                   (search_end - search_start) + ref_len, sample_rate, cancel)
    if window is None:
        return naive_overlap, None
    idx, score = best_alignment(ref, window)
    if idx is None:
        return naive_overlap, None
    return search_start + idx / sample_rate + ref_len, score


@dataclass
class PlanEntry:
    clip: ReplayClip
    trim_start: float
    notes: List[str] = field(default_factory=list)


def build_trim_plan(session, opts: MergeOptions, log: LogFn = None,
                    cancel: CancelToken = None) -> List[PlanEntry]:
    """How much to cut off the start of each clip so consecutive clips no
    longer overlap. Biased toward repeating a moment, never losing one."""
    log = log or (lambda m: None)
    plan = []
    prev_end = None
    for i, clip in enumerate(session):
        if cancel:
            cancel.check()
        notes = []
        if i == 0:
            trim_start = 0.0
        else:
            prev_clip = session[i - 1]
            trim_start = max(0.0, (prev_end - clip.start).total_seconds())
            trim_start = min(trim_start, max(0.0, clip.duration - 0.1))
            margin = opts.safety_margin
            if opts.audio_align and trim_start > 0:
                refined, conf = refine_overlap_with_audio(prev_clip, clip, trim_start,
                                                          margin=opts.align_margin, cancel=cancel)
                if conf is not None and conf >= 0.5:
                    refined = max(0.0, min(refined, max(0.0, clip.duration - 0.1)))
                    if abs(refined - trim_start) > 0.05:
                        notes.append(f"audio-aligned overlap {trim_start:.2f}s -> {refined:.2f}s "
                                     f"(confidence {conf:.2f})")
                    else:
                        notes.append(f"overlap {refined:.2f}s confirmed by audio (confidence {conf:.2f})")
                    trim_start = refined
                else:
                    if margin > 0:
                        margin = max(margin, 0.5)
                    if conf is not None:
                        notes.append(f"audio alignment low-confidence ({conf:.2f}); using timestamp "
                                     f"estimate {trim_start:.2f}s"
                                     + (" with a wider safety margin" if margin > 0 else ""))
                    else:
                        notes.append(f"overlap {trim_start:.2f}s from timestamps (no audio to check)")
            elif trim_start > 0:
                if margin > 0:
                    margin = max(margin, 0.5)
                notes.append(f"overlap {trim_start:.2f}s from timestamps")

            if trim_start > 0 and margin > 0:
                safe = max(0.0, trim_start - margin)
                if safe < trim_start - 0.001:
                    notes.append(f"safety margin: cutting {safe:.2f}s instead of {trim_start:.2f}s "
                                 f"(worst case ~{margin:.2f}s repeats)")
                trim_start = safe
            if trim_start > 0 and opts.extra_padding > 0:
                padded = max(0.0, trim_start - opts.extra_padding)
                if padded < trim_start - 0.001:
                    notes.append(f"extra padding: cutting {padded:.2f}s instead of {trim_start:.2f}s")
                trim_start = padded
        for n in notes:
            log(f"{clip.path.name}: {n}")
        plan.append(PlanEntry(clip, trim_start, notes))
        prev_end = max(prev_end, clip.end) if prev_end else clip.end
    return plan


def pick_lossless_cut(path: Path, trim_start: float, never_lose: bool = True,
                      max_repeat: float = 2.0):
    """Keyframe to cut at for a stream-copy trim. never_lose snaps backwards
    so extra footage is kept rather than lost; gives up (None) if that would
    repeat more than max_repeat seconds. Returns (cut, note) or (None, reason)."""
    kfs = ff.keyframe_times(path, around=trim_start)
    if not kfs:
        return None, "no keyframe information"
    before = [t for t in kfs if t <= trim_start + 0.001]
    after = [t for t in kfs if t >= trim_start - 0.001]
    if never_lose:
        if before:
            cut = before[-1]
            residual = trim_start - cut
            if residual > max_repeat:
                return None, f"nearest earlier keyframe is {residual:.2f}s back"
            note = (f"cut pulled back {residual:.2f}s to a keyframe (repeats {residual:.2f}s)"
                    if residual > 0.01 else "keyframe lands exactly on the cut")
            return cut, note
        return None, "no keyframe at or before the cut point"
    if after:
        cut = after[0]
        extra = cut - trim_start
        return cut, (f"cut moved forward {extra:.2f}s to a keyframe" if extra > 0.01
                     else "keyframe lands exactly on the cut")
    cut = kfs[-1]
    residual = trim_start - cut
    if residual > 0.5:
        return None, f"no keyframe within {residual:.2f}s after the cut point"
    return cut, f"nearest keyframe is {residual:.2f}s early"


def compute_splice_points(plan) -> List[Tuple[float, ReplayClip, ReplayClip]]:
    points, cumulative = [], 0.0
    for i, e in enumerate(plan):
        cumulative += e.clip.duration - e.trim_start
        if i < len(plan) - 1:
            points.append((cumulative, e.clip, plan[i + 1].clip))
    return points


# --------------------------------------------------------------------------
# Session overview (used by the GUI timeline and dry-run)
# --------------------------------------------------------------------------

@dataclass
class SessionPlan:
    clips: List[ReplayClip]           # every clip in the session, in order
    kept: List[ReplayClip]
    dropped: List[ReplayClip]

    @property
    def start(self) -> dt.datetime:
        return self.clips[0].start

    @property
    def end(self) -> dt.datetime:
        return max(c.end for c in self.clips)

    @property
    def name(self) -> str:
        return f"{self.start.strftime('%Y-%m-%d %H-%M-%S')} to {self.end.strftime('%H-%M-%S')}"


def plan_sessions(clips: List[ReplayClip], opts: MergeOptions, log: LogFn = None) -> List[SessionPlan]:
    out = []
    # Dropping a whole clip is irreversible, so the pruning margin never goes
    # below 0.25 s whatever the safety-margin setting says.
    prune_margin = max(opts.safety_margin, 0.25)
    for session in group_into_sessions(clips, opts.max_gap, log):
        kept, dropped = prune_redundant_clips(session, opts.max_gap, margin=prune_margin)
        for c in dropped:
            if log:
                log(f"{c.path.name} is fully contained in the clips around it (no unique footage); skipping it.")
        out.append(SessionPlan(session, kept, dropped))
    return out


# --------------------------------------------------------------------------
# Info files (clip-boundary logs)
# --------------------------------------------------------------------------

def _settings_lines(opts: MergeOptions):
    return [f"Safety margin: {'ON' if opts.safety_margin > 0 else 'OFF'} ({opts.safety_margin:.2f}s)",
            f"Extra trim padding: {opts.extra_padding:.2f}s",
            f"Audio alignment: {'ON' if opts.audio_align else 'OFF'}"]


def _section(title, body):
    return [title, "-" * len(title)] + body + [""]


def _map_time_in_merge(segments, t):
    if not segments:
        return None
    if t <= segments[0][0]:
        return segments[0][2]
    for i, (rs, re_, os_, oe) in enumerate(segments):
        if rs <= t <= re_:
            return os_ + (t - rs).total_seconds()
        if i + 1 < len(segments):
            next_rs = segments[i + 1][0]
            if re_ < t < next_rs:
                return oe if (t - re_) <= (next_rs - t) else segments[i + 1][2]
    return segments[-1][3]


def _map_time_in_trim(segments, t):
    if not segments:
        return None
    if t <= segments[0][0]:
        return segments[0][2], 0.0
    for i, (rs, re_, name, local_end) in enumerate(segments):
        if rs <= t <= re_:
            return name, (t - rs).total_seconds()
        if i + 1 < len(segments):
            next_rs = segments[i + 1][0]
            if re_ < t < next_rs:
                return (name, local_end) if (t - re_) <= (next_rs - t) else (segments[i + 1][2], 0.0)
    return segments[-1][2], segments[-1][3]


def trim_end_line(t, name, later, own=True):
    """The 'Original clip ends' line of a trim-mode info file."""
    t = format_hms_ms(t)
    if later == 0 and own:
        return f"    Original clip ends : {t} in this file"
    # A clip with no file of its own has nothing to count "later" from.
    return (f"    Original clip ends : {t} into {name}"
            + (f" ({later} file{'' if later == 1 else 's'} later)" if later and own else ""))


def clip_log_for_merge(session: SessionPlan, cuts: List[Tuple[ReplayClip, float]],
                       opts: MergeOptions, splice_note: str = "") -> str:
    """`cuts` = (clip, seconds actually cut off its start) for each kept clip."""
    lines = _section("Settings", _settings_lines(opts) + ([splice_note] if splice_note else []))
    trim_by = {id(c): t for c, t in cuts}
    dropped_ids = {id(c) for c in session.dropped}
    segments, cumulative = [], 0.0
    for clip, t in cuts:
        seg = max(0.0, clip.duration - t)
        segments.append((clip.start + dt.timedelta(seconds=t), clip.end, cumulative, cumulative + seg))
        cumulative += seg
    body = []
    for clip in session.clips:
        if id(clip) in dropped_ids:
            s = _map_time_in_merge(segments, clip.start)
            e = _map_time_in_merge(segments, clip.end)
            body.append(f"{clip.path.name}  (fully covered by neighbouring clips; not present as its own segment)")
            if s is not None and e is not None:
                body.append(f"    Would have spanned in merged video : {format_hms_ms(s)} - {format_hms_ms(e)}")
            body.append(f"    Its own original span                : {format_hms_ms(0)} - {format_hms_ms(clip.duration)}")
            if e is not None:
                body.append(f"    Original clip ends in merged video : {format_hms_ms(e)}")
            body.append("")
            continue
        t = trim_by[id(clip)]
        seg = max(0.0, clip.duration - t)
        out_start = _map_time_in_merge(segments, clip.start + dt.timedelta(seconds=t))
        note = "" if t <= 0.005 else f"  ({format_hms_ms(t)} trimmed off the start; duplicate of the previous clip)"
        body.append(clip.path.name)
        body.append(f"    Position in merged video : {format_hms_ms(out_start)} - {format_hms_ms(out_start + seg)}")
        body.append(f"    Span used from original  : {format_hms_ms(t)} - {format_hms_ms(clip.duration)}{note}")
        end_pos = _map_time_in_merge(segments, clip.end)
        if end_pos is not None:
            body.append(f"    Original clip ends in merged video : {format_hms_ms(end_pos)}")
        body.append("")
    lines += _section("Clip boundaries", body)
    return "\n".join(lines).rstrip() + "\n"


def clip_log_for_trim(session: SessionPlan, written: List[Tuple[ReplayClip, str, float]],
                      opts: MergeOptions) -> str:
    """`written` = (clip, output file name, seconds actually cut) per kept clip."""
    lines = _section("Settings", _settings_lines(opts))
    dropped_ids = {id(c) for c in session.dropped}
    info = {id(c): (name, t) for c, name, t in written}
    order = [name for _, name, _ in written]
    segments = []
    for clip, name, t in written:
        segments.append((clip.start + dt.timedelta(seconds=t), clip.end, name, max(0.0, clip.duration - t)))
    body = []
    for clip in session.clips:
        if id(clip) in dropped_ids:
            sm = _map_time_in_trim(segments, clip.start)
            em = _map_time_in_trim(segments, clip.end)
            body.append(f"{clip.path.name}  (fully covered by neighbouring clips; not exported as its own file)")
            if sm and em:
                if sm[0] == em[0]:
                    body.append(f"    Would have spanned within {sm[0]} : {format_hms_ms(sm[1])} - {format_hms_ms(em[1])}")
                else:
                    body.append(f"    Start would fall within {sm[0]} at : {format_hms_ms(sm[1])}")
                    body.append(f"    End would fall within {em[0]} at   : {format_hms_ms(em[1])}")
            body.append(f"    Its own original span                : {format_hms_ms(0)} - {format_hms_ms(clip.duration)}")
            if sm and em:
                body.append(trim_end_line(em[1], em[0], order.index(em[0]) - order.index(sm[0]), own=False))
            body.append("")
            continue
        name, t = info[id(clip)]
        note = "" if t <= 0.005 else f"  ({format_hms_ms(t)} trimmed off the start)"
        body.append(name)
        body.append(f"    Position in this trimmed video : {format_hms_ms(0)} - {format_hms_ms(clip.duration - t)}")
        body.append(f"    Span kept from original clip    : {format_hms_ms(t)} - {format_hms_ms(clip.duration)}{note}")
        em = _map_time_in_trim(segments, clip.end)
        if em:
            body.append(trim_end_line(em[1], em[0], order.index(em[0]) - order.index(name)))
        body.append("")
    lines += _section("Clip boundaries", body)
    return "\n".join(lines).rstrip() + "\n"
