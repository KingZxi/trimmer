"""Jobs: what the queue runs.

A ClipJob is one source file: optional trim segments, an optional
compress, a choice of audio tracks (and an optional mix), plus optional
track rips. A MergeJob is one OBS replay session: merge into one video or
trim overlaps into separate files, optionally compressed in the same pass.

Every output is written to "<name>.part<ext>", verified, and only then
renamed to its real name, so an interrupted run never leaves a broken file
that looks finished. Originals are only deleted (Recycle Bin by default)
after every output of the job has been verified.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import ffmpeg as ff
from . import merge as mg
from . import packets as pk
from .encode import (EncodeSettings, can_copy_audio, pick_settings, predict_size, quality_label, resolve_quality,
                     video_codec_args)
from .ffmpeg import CancelToken, Cancelled, FFmpegError, Progress
from .render import (AudioPlan, Span, copy_compatible, output_ext, render, rip_audio,
                     snap_spans_to_keyframes, verify_output)
from .smartcut import SmartCutError, smart_render
from .tag import build_tag, describe_encode, tag_args
from .util import (VIDEO_EXTENSIONS, format_hms, format_hms_ms, format_name_time, format_size, recycle_or_delete, safe_unlink, same_file,
                   sanitize_filename, trim_range_label, unique_path)

VIDEO_ONLY = " - video only"      # name extra of the video-only rip
NOT_COMPRESSED = " (NOT COMPRESSED)"    # kept uncompressed because compressing made it bigger

# Used when a cut must be frame-exact but the user didn't ask to compress:
# high quality H.264, same as the old merger's defaults.
EXACT_SETTINGS = EncodeSettings(mode="lossy", codec="h264", quality=18, preset="medium", audio="192")


@dataclass
class JobOptions:
    """Snapshot of the settings a job runs with, taken when it is queued."""
    output_dir: str = ""
    conflict_policy: str = "ask"
    delete_originals: bool = False
    permanent_delete: bool = False
    name_suffix: str = ""
    add_operation_tags: bool = True
    add_trim_range: bool = True          # the kept times in the "(trimmed ...)" name tag
    container: str = "same"
    exact_cuts: bool = False
    smart_cut: bool = True               # exact cuts re-encode only around each cut
    rip_format: str = "copy"
    # merge
    merge_mode: str = "merge"
    merge_method: str = "auto"
    max_gap: float = 1.5
    safety_margin: float = 0.25
    extra_trim_padding: float = 0.0
    audio_align: bool = True
    align_margin: float = 3.0
    seamless: bool = False
    lossless_max_repeat: float = 2.0
    timestamp_is_end: bool = False
    generate_clip_log: bool = False
    use_packet_matching: bool = True
    add_quality_label: bool = True       # "(LOSSY), (CRF23, H265, medium)" in compressed names

    @classmethod
    def from_settings(cls, s) -> "JobOptions":
        o = cls()
        for k in cls.__dataclass_fields__:
            if hasattr(s, k):
                setattr(o, k, getattr(s, k))
        o.safety_margin = (s.safety_margin if s.safety_margin_enabled else 0.0)
        if s.seamless:
            o.safety_margin = 0.0
        o.use_packet_matching = bool(getattr(s, "ui", {}).get("packet_matching", True))
        return o

    def merge_options(self) -> mg.MergeOptions:
        return mg.MergeOptions(max_gap=self.max_gap, safety_margin=self.safety_margin,
                               extra_padding=self.extra_trim_padding, audio_align=self.audio_align,
                               align_margin=self.align_margin, never_lose=not self.seamless,
                               lossless_max_repeat=self.lossless_max_repeat,
                               timestamp_is_end=self.timestamp_is_end)


@dataclass
class ClipJob:
    source: str
    segments: List[Tuple[float, Optional[float]]] = field(default_factory=list)
    make_video: bool = True
    audio_tracks: Optional[List[int]] = None     # None = all, [] = none
    mix_tracks: Optional[List[int]] = None
    volumes: dict = field(default_factory=dict)  # str(track) -> gain
    keep_mixed_originals: bool = True
    compress: bool = False
    encode: dict = field(default_factory=dict)
    rip_audio_tracks: List[int] = field(default_factory=list)
    rip_video: bool = False
    options: JobOptions = field(default_factory=JobOptions)
    output_name: str = ""                        # user's name for the outputs ("" = from the source)
    kind: str = "clip"

    @property
    def title(self) -> str:
        return self.output_name.strip() or Path(self.source).name

    def sources(self) -> List[str]:
        return [self.source]

    def summary(self) -> str:
        bits = []
        if self.segments:
            bits.append(f"trim ({len(self.segments)} part{'s' if len(self.segments) > 1 else ''})")
        if self.compress:
            bits.append("compress")
        if self.make_video and self.audio_tracks == []:
            bits.append("no audio")
        if self.mix_tracks:
            bits.append("mix audio")
        if self.rip_audio_tracks:
            bits.append(f"rip {len(self.rip_audio_tracks)} audio")
        if self.rip_video:
            bits.append("rip video")
        if not bits:
            bits.append("copy")
        if self.options.delete_originals:
            bits.append("delete original")
        return ", ".join(bits)


@dataclass
class MergeJob:
    clip_paths: List[str]
    compress: bool = False
    encode: dict = field(default_factory=dict)
    audio_tracks: Optional[List[int]] = None
    mix_tracks: Optional[List[int]] = None
    keep_mixed_originals: bool = True
    make_video: bool = True                      # the merged (or trimmed) video files
    rip_audio_tracks: List[int] = field(default_factory=list)
    rip_video: bool = False
    title_text: str = ""
    options: JobOptions = field(default_factory=JobOptions)
    output_name: str = ""                        # merged file (or trim-mode folder) name
    kind: str = "merge"

    @property
    def title(self) -> str:
        return (self.output_name.strip() or self.title_text
                or f"Replay session ({len(self.clip_paths)} clips)")

    def sources(self) -> List[str]:
        return list(self.clip_paths)

    def summary(self) -> str:
        bits = [self.options.merge_mode]
        if self.compress:
            bits.append("compress")
        if self.make_video and self.audio_tracks == []:
            bits.append("no audio")
        if self.mix_tracks:
            bits.append("mix audio")
        if not self.make_video:
            bits.append("no video file")
        if self.rip_audio_tracks:
            bits.append(f"rip {len(self.rip_audio_tracks)} audio")
        if self.rip_video:
            bits.append("rip video")
        if self.options.delete_originals:
            bits.append("delete originals")
        return ", ".join(bits)


def job_to_dict(job) -> dict:
    return asdict(job)


def job_from_dict(d: dict):
    d = dict(d)
    opts = JobOptions(**{k: v for k, v in (d.pop("options", None) or {}).items()
                         if k in JobOptions.__dataclass_fields__})
    cls = MergeJob if d.get("kind") == "merge" else ClipJob
    fields_ = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
    if cls is ClipJob:
        fields_["segments"] = [tuple(s) for s in fields_.get("segments", [])]
    return cls(options=opts, **fields_)


def job_key(job) -> str:
    """Identifies "this exact work on these exact files" for the resume record."""
    from .journal import source_key
    d = job_to_dict(job)
    d.pop("options", None)
    if isinstance(d.get("encode"), dict):
        d["encode"] = {k: v for k, v in d["encode"].items() if k != "chosen"}   # same work, analysed or not
    d["_sources"] = [source_key(s) for s in job.sources()]
    d["_out"] = job.options.output_dir
    return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class JobResult:
    status: str = "done"                  # done | reverted | failed | skipped | cancelled
    outputs: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    in_bytes: int = 0
    out_bytes: int = 0
    deleted: List[str] = field(default_factory=list)
    deleted_bytes: int = 0                # size of the originals that were really removed
    deferred: List[str] = field(default_factory=list)   # originals a later queued job still needs
    warnings: List[str] = field(default_factory=list)   # problems in the sources that didn't stop the job
    rip_from: str = ""                    # saved video still holding every source audio track untouched
    source_count: int = 0                 # clips this job read (a merge counts every replay)


class JobContext:
    """Callbacks from the engine to whoever runs it (GUI worker or CLI)."""

    def __init__(self, log: Callable[[str], None] = None,
                 progress: Callable[[str, Progress], None] = None,
                 resolve_conflict: Callable[[Path], str] = None,
                 cancel: CancelToken = None, still_needed: Callable[[Path], bool] = None):
        self.log = log or (lambda m: None)
        self.progress = progress or (lambda stage, p: None)
        self.resolve_conflict = resolve_conflict or (lambda p: "rename")
        self.cancel = cancel or CancelToken()
        # Truthy (True or a reason) for an original another queued job still reads,
        # or one whose other job didn't finish: its delete waits, or doesn't happen.
        self.still_needed = still_needed or (lambda p: False)
        # Asks whoever runs the job to let go of these files (close previews) before they're deleted.
        self.release: Callable[[List[Path]], None] = lambda paths: None
        self.skipped: List[str] = []      # outputs skipped by the name-clash policy
        self.warnings: List[str] = []     # source problems reported, not failed (frame gaps)
        self._sticky = None

    def stage(self, text: str):
        self.progress(text, Progress(None, 0, None, None, None))

    def reporter(self, text: str):
        return lambda p: self.progress(text, p)


class _Skip(Exception):
    pass


# --------------------------------------------------------------------------
# Output paths
# --------------------------------------------------------------------------

def _out_dir(job, source: Path) -> Path:
    d = Path(job.options.output_dir) if job.options.output_dir else source.parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def _claim(path: Path, ctx: JobContext, opts: JobOptions, sources: List[Path]) -> Path:
    """Apply the name-clash policy. Never returns a source file's path."""
    if any(same_file(path, s) for s in sources):
        return unique_path(path.with_name(f"{path.stem} (edited){path.suffix}"))
    if not path.exists():
        return path
    policy = ctx._sticky or opts.conflict_policy
    if policy == "ask":
        answer = ctx.resolve_conflict(path)
        if answer.endswith("_all"):
            ctx._sticky = answer[:-4]
            answer = ctx._sticky
        policy = answer
    if policy == "overwrite":
        return path
    if policy == "skip":
        ctx.skipped.append(path.name)
        raise _Skip(f"{path.name} already exists; skipped")
    if policy == "cancel":
        raise Cancelled()
    return unique_path(path)


def _part(path: Path) -> Path:
    return path.with_name(f"{path.stem}.part{path.suffix}")


def _reject(ctx, part: Path, final: Path, problem: str, what: str = "Output failed verification"):
    """A saved file failed its check: delete it and fail the job (originals are never touched then).
    When it only came out short, the source is usually damaged partway (a recording cut off by a
    crash): the part that could be read is kept, clearly named, so the good footage isn't lost."""
    m = re.match(r"output is ([0-9.]+)s long but ([0-9.]+)s was expected", problem or "")
    got = float(m.group(1)) if m else 0.0
    if m and 10 <= got < float(m.group(2)) and part.exists():
        partial = unique_path(final.with_name(f"{final.stem} (partial, first {format_name_time(got)}){final.suffix}"))
        try:
            os.replace(part, partial)
        except OSError:
            partial = None
        if partial is not None:
            ctx.log(f"Kept what could be saved as {partial.name}")
            raise FFmpegError(f"{what}: {problem}. The source is probably damaged after "
                              f"{format_hms(got)}: the part before it was saved as \"{partial.name}\". "
                              "The original was kept.")
    safe_unlink(part)
    raise FFmpegError(f"{what}: {problem}")


def _finish(part: Path, final: Path, date_source: Path = None):
    """Rename a verified .part file to its real name, keeping the original's
    modified date so clips still sort by when they were recorded."""
    os.replace(part, final)
    if date_source is not None:
        try:
            st = Path(date_source).stat()
            os.utime(final, (st.st_atime, st.st_mtime))
        except OSError:
            pass


def _qlabel(opts, *args) -> str:
    """Compression details for the file name, unless turned off in Settings."""
    return quality_label(*args) if opts.add_quality_label else ""


def _base_stem(job: ClipJob, src: Path) -> str:
    """The user's chosen name, else the source name plus the configured suffix."""
    name = job.output_name.strip()
    if Path(name).suffix.lower() in VIDEO_EXTENSIONS | {".wav", ".flac", ".mp3", ".m4a"}:
        name = Path(name).stem          # "Highlight.mp4" -> "Highlight"; the extension is ours to pick
    name = sanitize_filename(name) if name else ""
    return name or sanitize_filename(src.stem + job.options.name_suffix)


def _trim_tag(opts: JobOptions, ranges) -> str:
    """'(trimmed 1m05s-1m32s)' for the kept (start, stop) ranges, or "" when
    nothing was cut or the times are turned off in Settings."""
    if not (opts.add_operation_tags and opts.add_trim_range and ranges):
        return ""
    return f" ({trim_range_label(ranges)})"


def _tags(job: ClipJob, spans: List[Span]) -> str:
    if not job.options.add_operation_tags:
        return ""
    if job.output_name.strip():     # a typed name only gains the kept times
        return _trim_tag(job.options, [(s.start, s.stop) for s in spans] if job.segments else [])
    t = []
    if job.audio_tracks == [] and job.make_video:
        t.append("no audio")
    if job.mix_tracks:
        t.append("mixed audio")
    rest = f" ({', '.join(t)})" if t else ""
    if not job.segments:
        return rest
    if not job.options.add_trim_range:
        return f" ({', '.join(['trimmed'] + t)})"
    # The range has its own commas, so the other tags go in their own brackets.
    return _trim_tag(job.options, [(s.start, s.stop) for s in spans]) + rest


def _clip_tag(job: ClipJob, info, audio: AudioPlan, enc=None, q=None, video_only: bool = False) -> str:
    """The hidden Trimmer tag for a clip job's video output. `enc` is only
    passed when the output really is compressed."""
    ops = []
    if job.segments:
        ops.append("trimmed")
    if enc is not None:
        ops.append(f"compressed ({describe_encode(enc, q)})")
    if video_only:
        ops.append("video only")
    else:
        if len([t for t in (audio.mix or []) if t in audio.selected(len(info.audio))]) >= 2:
            ops.append("mixed audio")
        if audio.tracks is not None and len(audio.selected(len(info.audio))) < len(info.audio):
            ops.append("removed audio tracks")
    return build_tag(info.trimmer_tag, ops)


def _merge_tag(job: MergeJob, previous: str, audio: AudioPlan, n_tracks: int, enc=None, q=None) -> str:
    """The hidden Trimmer tag for an output of a replay session. `enc` is
    only passed when the output really is compressed."""
    ops = ["trimmed replay session" if job.options.merge_mode == "trim" else "merged (replay session)"]
    if enc is not None:
        ops.append(f"compressed ({describe_encode(enc, q)})")
    sel = audio.selected(n_tracks)
    if len([t for t in (audio.mix or []) if t in sel]) >= 2:
        ops.append("mixed audio")
    if audio.tracks is not None and len(sel) < n_tracks:
        ops.append("removed audio tracks")
    return build_tag(previous, ops)


def _check_space(folder: Path, need: int):
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return
    if need > free:
        raise FFmpegError(f"Not enough free space in {folder}: about {format_size(need)} needed, "
                          f"{format_size(free)} free.")


# --------------------------------------------------------------------------
# Integrity ("nothing lost") checks
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Clip jobs
# --------------------------------------------------------------------------

def _spans(job: ClipJob, info) -> List[Span]:
    src = Path(job.source)
    if not job.segments:
        return [Span(src, info)]
    out = []
    for s, e in job.segments:
        s = max(0.0, float(s))
        e = None if e is None or e >= info.duration - 0.0005 else float(e)
        if e is not None and e <= s + 0.05:
            continue
        out.append(Span(src, info, s, e))
    if not out:
        raise FFmpegError("The trim selection is empty.")
    return out


def _audio_plan(job, n_tracks) -> AudioPlan:
    vols = {int(k): float(v) for k, v in (getattr(job, "volumes", None) or {}).items()}
    return AudioPlan(tracks=job.audio_tracks, mix=job.mix_tracks, volumes=vols,
                     keep_originals=job.keep_mixed_originals)


def _n_out_audio(plan: AudioPlan, n_avail: int) -> int:
    sel = plan.selected(n_avail)
    mix = [t for t in (plan.mix or []) if t in sel]
    if len(mix) >= 2:
        return 1 + len(sel) - (0 if plan.keep_originals else len(mix))
    return len(sel)


def _write_video(job: ClipJob, ctx: JobContext, info, spans: List[Span], out_dir: Path,
                 name_extra: str, audio: AudioPlan, res: JobResult, label_cache: dict) -> Optional[Path]:
    """One video output from spans, by copy or encode. Returns the final path."""
    src = Path(job.source)
    opts = job.options
    trimmed = bool(job.segments)
    ext = output_ext(src, opts.container)
    stem = _base_stem(job, src)
    kept = sum(s.length for s in spans)
    enc = EncodeSettings.from_dict(job.encode) if job.compress else None
    needs_exact = trimmed and opts.exact_cuts and enc is None
    claimed = None
    if needs_exact and opts.smart_cut:
        claimed = _claim(out_dir / f"{stem}{_tags(job, spans)}{name_extra}{ext}", ctx, opts, [src])
        done = _write_smart(job, ctx, info, spans, claimed, audio,
                            _clip_tag(job, info, audio, video_only=name_extra == VIDEO_ONLY))
        if done is not None:
            _note_rip_source(res, done, info, audio, name_extra)
            return done
    if needs_exact:
        enc = EncodeSettings.from_dict(EXACT_SETTINGS.to_dict())

    if enc is not None:
        problems = enc.problems()
        if problems:
            raise FFmpegError(" ".join(problems))
        if "q" not in label_cache:
            if enc.is_auto:
                ctx.stage(f"Analysing quality ({enc.quality})")
            label_cache["q"] = resolve_quality(src, info, enc, ctx.cancel, ctx.log)
        q, method = label_cache["q"]
        label = _qlabel(job.options, enc, q, method) if job.compress else ""
        if q is not None and job.compress:
            ctx.log(f"Using {'CQ' if enc.encoder != 'software' else 'CRF'} {q}"
                    + (f" (chosen by {method})" if method else ""))
        final = claimed or _claim(out_dir / f"{stem}{_tags(job, spans)}{name_extra}{label}{ext}",
                                  ctx, opts, [src])
        part = _part(final)
        tag = _clip_tag(job, info, audio, enc if job.compress else None, q, name_extra == VIDEO_ONLY)
        original_equiv = int(info.size * (kept / info.duration)) if info.duration else info.size
        limit = original_equiv if (job.compress and enc.revert_if_larger) else None
        r = None
        if limit is not None and enc.mode == "lossy" and q is not None:
            ctx.stage("Predicting output size")
            guess = predict_size(src, info, enc, q, kept, ctx.cancel)
            if guess > limit * 1.05:
                ctx.log(f"Predicted size {format_size(guess)} is larger than the original "
                        f"({format_size(limit)}); skipping the encode.")
                r = ff.RunResult("too_large", "", guess)
        if r is None:
            r = render(spans, part, encode=enc, quality=q, audio=audio,
                       on_progress=ctx.reporter("Encoding"), cancel=ctx.cancel, size_limit=limit, log=ctx.log,
                       tag=tag)
        if r.status == "too_large":
            ctx.log(f"Compressed output would be larger than the original ({format_size(r.last_size)} vs "
                    f"{format_size(original_equiv)}); keeping the original footage instead.")
            res.status = "reverted"
            _snap(ctx, spans)
            final = _claim(out_dir / f"{stem}{_tags(job, spans)}{name_extra}{NOT_COMPRESSED}{ext}",
                           ctx, opts, [src])
            return _write_copy(job, ctx, info, spans, final, audio, res,
                               _clip_tag(job, info, audio, video_only=name_extra == VIDEO_ONLY))
        if not r.ok:
            raise FFmpegError(f"ffmpeg failed:\n{r.stderr_tail}")
        expected = kept
        n_audio = _n_out_audio(audio, len(info.audio))
        ctx.stage("Verifying")
        problem = verify_output(part, expected, True, n_audio)
        if problem:
            _reject(ctx, part, final, problem)
        _verify_frames(ctx, part, expected, info.fps, _span_windows(spans), "Output")
        _finish(part, final, src)
        return final

    _snap(ctx, spans)       # before naming, so the kept times in the name are the real ones
    final = _claim(out_dir / f"{stem}{_tags(job, spans)}{name_extra}{ext}", ctx, opts, [src])
    done = _write_copy(job, ctx, info, spans, final, audio, res,
                       _clip_tag(job, info, audio, video_only=name_extra == VIDEO_ONLY))
    _note_rip_source(res, done, info, audio, name_extra)
    return done


def _note_rip_source(res: JobResult, video: Path, info, audio: AudioPlan, name_extra: str):
    """Remember a saved video whose audio tracks are the source's, copied
    untouched: audio rips taken from it line up with its picture exactly."""
    if name_extra == "" and audio.tracks is None and not audio.needs_filter \
            and all(can_copy_audio(t.codec, video.suffix.lower()) for t in info.audio):
        res.rip_from = str(video)


def _write_smart(job: ClipJob, ctx, info, spans: List[Span], final: Path,
                 audio: AudioPlan, tag: str = "") -> Optional[Path]:
    """Frame-exact trim by smart cut. Returns the final path, or None when
    smart cut can't be used here (the caller then re-encodes the trim)."""
    src = Path(job.source)
    part = _part(final)
    ctx.stage("Smart cut")
    try:
        r = smart_render([Span(s.path, s.info, s.start, s.end) for s in spans], part, audio,
                         on_progress=ctx.reporter("Smart cut"), cancel=ctx.cancel, log=ctx.log, tag=tag)
    except SmartCutError as e:
        safe_unlink(part)
        ctx.log(f"Smart cut not possible ({e}); re-encoding the trim instead.")
        return None
    if not r.ok:
        ctx.log(f"Smart cut failed; re-encoding the trim instead.\n{r.stderr_tail[-400:]}")
        return None
    ctx.stage("Verifying")
    problem = verify_output(part, sum(s.length for s in spans), True, _n_out_audio(audio, len(info.audio)))
    if problem:
        safe_unlink(part)
        ctx.log(f"Smart cut output failed verification ({problem}); re-encoding the trim instead.")
        return None
    _note_source_gaps(ctx, _span_windows(spans), info.fps)
    _finish(part, final, src)
    return final


def _snap(ctx, spans: List[Span]):
    """Move each start back to its keyframe (a stream copy starts there anyway).
    Mutates spans, so the caller sees where the video really starts."""
    for note in snap_spans_to_keyframes(spans):
        ctx.log(note)


def _write_copy(job: ClipJob, ctx, info, spans: List[Span], final: Path, audio: AudioPlan,
                res: JobResult, tag: str = "") -> Path:
    """Stream copy of spans already snapped to keyframes (see _snap)."""
    src = Path(job.source)
    part = _part(final)
    want = [(s.start, s.end) for s in spans]
    whole = len(spans) == 1 and spans[0].start <= 0.0005 and spans[0].end is None
    if whole and audio.tracks is None and not audio.needs_filter and not tag \
            and final.suffix.lower() == src.suffix.lower():
        ctx.stage("Copying")
        shutil.copyfile(src, part)
        ctx.cancel.check()
    else:
        r = render(spans, part, encode=None, quality=None, audio=audio,
                   on_progress=ctx.reporter("Copying (no re-encode)"), cancel=ctx.cancel, tag=tag)
        if not r.ok:
            raise FFmpegError(f"ffmpeg failed:\n{r.stderr_tail}")
    ctx.stage("Verifying")
    expected = sum(s.length for s in spans)
    problem = verify_output(part, expected, True, _n_out_audio(audio, len(info.audio)))
    if not problem and len(spans) == 1 and not whole:
        problem = check_copy_trim(src, part, want[0][0], want[0][1])
    if problem:
        _reject(ctx, part, final, problem)
    _note_source_gaps(ctx, _span_windows(spans), info.fps)
    _finish(part, final, src)
    return final


def _write_rip(ctx: JobContext, opts: JobOptions, spans: List[Span], t: int, name: str, out_dir: Path,
               sources: List[Path], date_source: Path) -> Path:
    """One audio track of the spans as its own file, verified before it gets its name."""
    fmt = opts.rip_format
    if fmt == "copy" and len(spans) > 1:
        fmt = "flac"     # joining several parts needs a re-encode; keep it lossless
    ctx.stage(f"Ripping audio track {t + 1}")
    tmp_base = out_dir / f"{name}.part"
    ops = (["trimmed"] if any(s.start > 0.0005 or (s.end is not None and s.end < s.info.duration - 0.0005)
                              for s in spans) else []) + ["audio rip"]
    tag = build_tag(spans[0].info.trimmer_tag, ops)
    r, tmp_path = rip_audio(spans, t, fmt, tmp_base, ctx.reporter(f"Ripping audio track {t + 1}"), ctx.cancel,
                            tag=tag)
    if not r.ok:
        raise FFmpegError(f"ffmpeg failed ripping track {t + 1}:\n{r.stderr_tail}")
    final = _claim(out_dir / f"{name}{tmp_path.suffix}", ctx, opts, sources)
    problem = verify_output(tmp_path, sum(s.length for s in spans), expect_video=False, n_audio=1)
    if problem:
        safe_unlink(tmp_path)
        raise FFmpegError(f"Track {t + 1} failed verification: {problem}")
    _finish(tmp_path, final, date_source)
    return final


def run_clip_job(job: ClipJob, ctx: JobContext) -> JobResult:
    res = JobResult()
    src = Path(job.source)
    if not src.exists():
        raise FFmpegError(f"{src.name} no longer exists.")
    ctx.stage("Reading clip")
    info = ff.probe(src, use_cache=False)
    res.in_bytes = info.size
    res.source_count = 1
    out_dir = _out_dir(job, src)
    _check_space(out_dir, info.size)
    spans = _spans(job, info)
    if job.segments:
        ctx.log("Keeping " + ", ".join(f"{format_hms_ms(s.start)}-{format_hms_ms(s.stop)}" for s in spans))
    label_cache = {}
    outputs: List[Path] = []
    audio = _audio_plan(job, len(info.audio))

    skips_before = len(ctx.skipped)

    def attempt(fn, *a):
        try:
            p = fn(*a)
            if p is not None:
                outputs.append(p)
                ctx.log(f"Saved {p.name}")
            return p
        except _Skip as e:
            ctx.log(str(e))
            res.messages.append(str(e))

    # Audio rips cover exactly what the saved video covers. A stream copy
    # starts each part on the keyframe before the mark (_write_video moves
    # these spans there too), and when the video kept the source's audio
    # untouched the rips are taken straight from it.
    kept_spans = spans
    if job.make_video and info.has_video:
        vspans = [Span(s.path, s.info, s.start, s.end) for s in spans]
        if attempt(_write_video, job, ctx, info, vspans, out_dir, "", audio, res, label_cache) is not None:
            kept_spans = vspans
    if job.rip_video and info.has_video:
        vspans = [Span(s.path, s.info, s.start, s.end) for s in spans]
        if attempt(_write_video, job, ctx, info, vspans, out_dir, VIDEO_ONLY, AudioPlan(tracks=[]), res,
                   label_cache) is not None and kept_spans is spans:
            kept_spans = vspans
    rip_spans = kept_spans
    if job.rip_audio_tracks and job.segments and res.rip_from:
        made = Path(res.rip_from)
        rip_spans = [Span(made, ff.probe(made, use_cache=False), 0.0, None)]
    if job.rip_audio_tracks and any(abs(a.start - b.start) > 0.0005 for a, b in zip(kept_spans, spans)):
        ctx.log("Audio rips start where the video does (on the keyframe), so they line up with it.")
    for t in job.rip_audio_tracks:
        if t >= len(info.audio):
            continue
        tr = info.audio[t]
        trim = _trim_tag(job.options, [(s.start, s.stop) for s in kept_spans] if job.segments else [])
        name = sanitize_filename(f"{_base_stem(job, src)}{trim} - Track {t + 1}"
                                 + (f" ({tr.title})" if tr.title else ""))
        attempt(_write_rip, ctx, job.options, rip_spans, t, name, out_dir, [src], src)

    res.outputs = [str(p) for p in outputs]
    res.out_bytes = sum(p.stat().st_size for p in outputs if p.exists())
    if not outputs:
        res.status = "skipped"
        return res
    if job.options.delete_originals:
        if len(ctx.skipped) > skips_before:
            _keep_note(ctx, res, [src])
        else:
            _delete_sources(job, [src], outputs, ctx, res)
    return res


def _keep_note(ctx, res: JobResult, sources: List[Path]):
    """An output was skipped (its name was taken), so not everything asked for
    was saved here and checked: the originals stay."""
    msg = (f"Original{'s' if len(sources) > 1 else ''} kept ({', '.join(Path(s).name for s in sources)}): "
           f"not every output was saved, because a file with its name already existed.")
    ctx.log(msg)
    res.messages.append(msg)


def _delete_sources(job, sources: List[Path], outputs: List[Path], ctx, res: JobResult):
    out_set = {str(Path(o).resolve()) for o in outputs}
    doomed = []
    for s in sources:
        if str(Path(s).resolve()) in out_set:
            continue
        why = ctx.still_needed(Path(s))
        if why:
            res.deferred.append(str(s))
            ctx.log(f"Original {Path(s).name} kept for now: "
                    + (why if isinstance(why, str) else "a later job in the queue still uses it") + ". "
                    "It is removed once every job using it has saved its outputs.")
            continue
        doomed.append(Path(s))
    if doomed:
        ctx.release(doomed)
    for s in doomed:
        try:
            size = Path(s).stat().st_size
            what = recycle_or_delete(s, permanent=job.options.permanent_delete)
            res.deleted.append(str(s))
            res.deleted_bytes += size
            ctx.log(f"Original {Path(s).name} {what}")
        except OSError as e:
            msg = f"Couldn't remove original {Path(s).name}: {e}"
            ctx.log(msg)
            res.messages.append(msg)


# --------------------------------------------------------------------------
# Replay merge jobs
# --------------------------------------------------------------------------

def run_merge_job(job: MergeJob, ctx: JobContext) -> JobResult:
    res = JobResult()
    opts = job.options
    mopts = opts.merge_options()
    paths = [Path(p) for p in job.clip_paths]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        raise FFmpegError(f"These clips no longer exist: {', '.join(missing)}")
    ctx.stage("Reading clips")
    clips, skipped = mg.load_clips(paths, mopts, ctx.log, ctx.cancel)
    if not clips:
        raise FFmpegError("None of the clips could be read.")
    res.in_bytes = sum(c.info.size for c in clips)
    res.source_count = len(clips)
    out_root = _out_dir(job, clips[0].path)
    _check_space(out_root, res.in_bytes)
    sessions = mg.plan_sessions(clips, mopts, ctx.log)
    outputs: List[Path] = []
    complete: List[Path] = []       # clips of sessions where every output was saved
    incomplete: List[Path] = []
    for si, session in enumerate(sessions, 1):
        ctx.cancel.check()
        ctx.log(f"Session {si}: {len(session.clips)} clip(s), {session.name}")
        skips_before = len(ctx.skipped)
        made = []
        try:
            name = job.output_name.strip()
            if name and len(sessions) > 1:
                name = f"{name} ({si})"
            made = _run_session(job, ctx, session, out_root, res, name)
            outputs += made
        except _Skip as e:
            ctx.log(str(e))
            res.messages.append(str(e))
        whole = made and len(ctx.skipped) == skips_before
        (complete if whole else incomplete).extend(c.path for c in session.clips)
    res.outputs = [str(p) for p in outputs]
    res.out_bytes = sum(p.stat().st_size for p in outputs if p.exists())
    if not outputs:
        res.status = "skipped"
        return res
    if opts.delete_originals:
        if incomplete:
            _keep_note(ctx, res, incomplete)
        _delete_sources(job, complete, outputs, ctx, res)
    return res


def _merge_encode(job: MergeJob) -> Optional[EncodeSettings]:
    if not job.compress:
        return None
    enc = EncodeSettings.from_dict(job.encode)
    problems = enc.problems()
    if problems:
        raise FFmpegError(" ".join(problems))
    return enc


def session_names(session: mg.SessionPlan):
    """Default (merged file name, trim-mode folder name) for a session."""
    s = session.start.strftime("%Y-%m-%d %H-%M-%S")
    e = session.end.strftime("%H-%M-%S")
    return f"Merged {s} to {e}", f"Session {s} to {e}"


def _run_session(job: MergeJob, ctx: JobContext, session: mg.SessionPlan, out_root: Path,
                 res: JobResult, custom_name: str = "") -> List[Path]:
    opts = job.options
    merged_name, folder_name = session_names(session)
    if custom_name:             # the user's name is used as-is, without the suffix setting
        merged_name = folder_name = sanitize_filename(custom_name)
        opts = JobOptions(**{**asdict(opts), "name_suffix": ""})
        job = replace(job, options=opts)

    if len(session.clips) == 1:
        c = session.clips[0]
        ctx.log("Single clip in this session; nothing to merge.")
        cj = ClipJob(source=str(c.path), compress=job.compress, encode=job.encode,
                     audio_tracks=job.audio_tracks, mix_tracks=job.mix_tracks,
                     keep_mixed_originals=job.keep_mixed_originals, make_video=job.make_video,
                     rip_audio_tracks=job.rip_audio_tracks, rip_video=job.rip_video,
                     output_name=custom_name if opts.merge_mode != "trim" else "",
                     options=JobOptions(**{**asdict(opts), "delete_originals": False,
                                           "add_operation_tags": False}))
        if opts.merge_mode == "trim":
            d = out_root / folder_name
            d.mkdir(parents=True, exist_ok=True)
            cj.options.output_dir = str(d)
        r = run_clip_job(cj, ctx)
        return [Path(p) for p in r.outputs]

    outputs = []
    if job.make_video:
        outputs = _build_session(job, ctx, session, out_root, merged_name, folder_name)
    if job.rip_audio_tracks or job.rip_video:
        outputs += _rip_session(job, ctx, session, out_root, merged_name, folder_name, outputs)
    return outputs


def _build_session(job, ctx, session: mg.SessionPlan, out_root, merged_name, folder_name) -> List[Path]:
    """The merged video (or the trimmed videos) of a multi-clip session."""
    opts = job.options
    enc = _merge_encode(job)
    audio = AudioPlan(tracks=job.audio_tracks, mix=job.mix_tracks, keep_originals=job.keep_mixed_originals)

    # 1) Exact packet matching, when the clips share encoded packets.
    if opts.use_packet_matching and opts.merge_method != "exact":
        ctx.stage("Matching clips frame by frame")
        fps = session.clips[0].info.fps or 60.0
        pj = pk.join_by_packets(session.clips, fps=fps, cancel=ctx.cancel)
        for n in pj.notes:
            ctx.log(n)
        if pj.ok:
            ctx.log("Exact overlap found by matching packets: no footage repeats or is lost.")
            return _packet_session(job, ctx, session, pj, out_root, merged_name, folder_name, enc, audio)
        ctx.log("Packets don't match exactly; using the audio and timestamp method instead.")

    # 2) Fallback: the old estimate + audio alignment + safety margins.
    return _fallback_session(job, ctx, session, out_root, merged_name, folder_name, enc, audio)


def _rip_session(job: MergeJob, ctx: JobContext, session: mg.SessionPlan, out_root: Path, merged_name: str,
                 folder_name: str, built: List[Path]) -> List[Path]:
    """Audio and video-only rips of a multi-clip session: one set per merged
    video, or per trimmed clip. They come from the real outputs when those
    still hold every track untouched, else from a lossless copy of the join."""
    opts = job.options
    srcs = [c.path for c in session.clips]
    date_src = session.clips[-1].path
    dest = out_root / folder_name if opts.merge_mode == "trim" else out_root
    outputs: List[Path] = []

    def attempt(fn, *a):
        try:
            outputs.append(fn(*a))
            ctx.log(f"Saved {outputs[-1].name}")
        except _Skip as e:
            ctx.log(str(e))

    def rip_unit(path: Path, d: Path, stem: str):
        ctx.cancel.check()
        info = ff.probe(path, use_cache=False)
        span = Span(path, info, 0.0, None)
        for t in job.rip_audio_tracks:
            if t >= len(info.audio):
                continue
            tr = info.audio[t]
            name = sanitize_filename(f"{stem} - Track {t + 1}" + (f" ({tr.title})" if tr.title else ""))
            attempt(_write_rip, ctx, opts, [span], t, name, d, srcs, date_src)
        if job.rip_video and info.has_video:
            def do_video():
                ctx.stage("Ripping video")
                final = _claim(d / f"{stem}{VIDEO_ONLY}{path.suffix}", ctx, opts, srcs)
                part = _part(final)
                r = render([Span(path, info, 0.0, None)], part, encode=None, quality=None,
                           audio=AudioPlan(tracks=[]), on_progress=ctx.reporter("Ripping video"),
                           cancel=ctx.cancel, log=ctx.log,
                           tag=build_tag(info.trimmer_tag, ["video only"]))
                if not r.ok:
                    raise FFmpegError(f"ffmpeg failed ripping the video:\n{r.stderr_tail}")
                problem = verify_output(part, info.duration, True, 0)
                if problem:
                    safe_unlink(part)
                    raise FFmpegError(f"Video-only file failed verification: {problem}")
                _finish(part, final, date_src)
                return final
            attempt(do_video)

    if built and not job.compress and job.audio_tracks is None and not job.mix_tracks:
        for p in built:
            rip_unit(p, p.parent, p.stem)
        return outputs
    # With no video saved, nothing else writes the info file, so the hidden
    # build writes it and it is copied next to the rips.
    want_log = opts.generate_clip_log and not job.make_video
    with tempfile.TemporaryDirectory(prefix="trimmer_rip_") as td:
        ctx.stage("Preparing the tracks to rip")
        plain = replace(job, compress=False, audio_tracks=None, mix_tracks=None, make_video=True,
                        rip_audio_tracks=[], rip_video=False,
                        options=JobOptions(**{**asdict(opts), "generate_clip_log": want_log,
                                              "delete_originals": False}))
        made = _build_session(plain, ctx, session, Path(td), merged_name, folder_name)
        dest.mkdir(parents=True, exist_ok=True)
        for p in made:
            rip_unit(p, dest, p.stem)
        if want_log and outputs and made:
            if opts.merge_mode == "trim":
                log, final = made[0].parent / "info.txt", dest / "info.txt"
                note = ("No video was saved: each entry below matches the tracks ripped from it, "
                        "named after it with \" - Track N\" or \" - video only\".")
            else:
                log = made[0].with_name(f"{made[0].stem} - info.txt")
                final = dest / f"{made[0].stem} - info.txt"
                note = "No video was saved: the times below apply to the tracks ripped from this session."
            try:
                text = log.read_text(encoding="utf-8")
            except OSError:
                text = None
            if text is not None:
                _write_text(final, f"{note}\n\n{text}")
    return outputs


def _packet_session(job, ctx, session, pj: pk.PacketJoin, out_root, merged_name, folder_name,
                    enc, audio) -> List[Path]:
    opts = job.options
    first = pj.pieces[0].clip
    ext = output_ext(first.path, opts.container)
    srcs = [c.path for c in session.clips]
    outputs = []
    mark = len(ctx.warnings)
    with tempfile.TemporaryDirectory(prefix="trimmer_join_") as td:
        if opts.merge_mode == "trim":
            folder = out_root / folder_name
            folder.mkdir(parents=True, exist_ok=True)
            written = []
            for i, p in enumerate(pj.pieces):
                ctx.cancel.check()
                single = pk.PacketJoin([pk.Piece(p.clip, p.video, p.audio, p.stop, p.audio_stop, None)], [], True)
                kept_s = p.video.pts[p.stop] if p.stop is not None else p.clip.duration
                trim = _trim_tag(opts, [(0.0, kept_s)] if p.stop is not None else [])
                outputs.append(_packet_output(job, ctx, single, folder, p.clip.path.stem, ext, enc, audio, srcs,
                                              Path(td) / str(i), stage=f"Clip {i + 1} of {len(pj.pieces)}",
                                              trim=trim))
                written.append((p.clip, outputs[-1].name, kept_s))
            if opts.generate_clip_log:
                _write_text(folder / "info.txt", _packet_log(session, pj, written, trim_mode=True)
                            + _gap_section(ctx.warnings[mark:]))
            return outputs
        out = _packet_output(job, ctx, pj, out_root, merged_name, ext, enc, audio, srcs, Path(td),
                             stage="Merging")
        outputs.append(out)
        if opts.generate_clip_log:
            _write_text(out.with_name(f"{out.stem} - info.txt"), _packet_log(session, pj, None, False)
                        + _gap_section(ctx.warnings[mark:]))
        _log_splices_packets(ctx, pj)
        return outputs


def _plain_limit(ctx, enc, audio: AudioPlan, plain: int, q, src: Path, info, seconds: float):
    """With "keep the original if compressing would make it bigger" on, the
    size a compressed merge must stay under (a plain merge's), or "predicted"
    when the sample encode already shows it won't. None = no limit."""
    if enc is None or not enc.revert_if_larger or enc.mode == "target_size" or audio.needs_filter:
        return None
    if enc.mode == "lossy" and q is not None:
        ctx.stage("Predicting output size")
        guess = predict_size(src, info, enc, q, seconds, ctx.cancel)
        if guess > plain * 1.05:
            ctx.log(f"Predicted size {format_size(guess)} is larger than a plain merge "
                    f"({format_size(plain)}); merging without compressing instead.")
            return "predicted"
    return plain


def _too_large_note(ctx, r, plain: int):
    ctx.log(f"Compressed output would be larger than a plain merge ({format_size(r.last_size)} vs "
            f"{format_size(plain)}); merging without compressing instead.")


def _packet_output(job, ctx, pj: pk.PacketJoin, folder: Path, stem: str, ext: str, enc, audio: AudioPlan,
                   srcs, tmpdir: Path, stage: str, trim: str = "", extra: str = "") -> Path:
    tmpdir.mkdir(parents=True, exist_ok=True)
    vlist, alist = pk.write_concat_lists(pj, tmpdir)
    info = pj.pieces[0].clip.info
    n_avail = min(len(p.clip.info.audio) for p in pj.pieces)
    selected = audio.selected(n_avail)
    total = pj.total_duration
    label = ""
    q = None
    limit = None
    if enc is not None:
        q, method = resolve_quality(pj.pieces[0].clip.path, info, enc, ctx.cancel, ctx.log) \
            if enc.mode == "lossy" else (None, None)
        label = _qlabel(job.options, enc, q, method)
        limit = _plain_limit(ctx, enc, audio, pj.copy_bytes(), q, pj.pieces[0].clip.path, info, total)
        if limit == "predicted":
            return _packet_output(job, ctx, pj, folder, stem, ext, None, audio, srcs, tmpdir, stage, trim,
                                  NOT_COMPRESSED)
    final = _claim(folder / f"{sanitize_filename(stem + job.options.name_suffix)}{trim}{extra}{label}{ext}",
                   ctx, job.options, srcs)
    part = _part(final)
    tag = _merge_tag(job, info.trimmer_tag, audio, n_avail, enc, q)
    args = ["-f", "concat", "-safe", "0", "-i", str(vlist)]
    a_in = 0
    if alist is not None and selected:
        args += ["-f", "concat", "-safe", "0", "-i", str(alist)]
        a_in = 1
    if enc is None and not audio.needs_filter:
        args += ["-map", "0:v:0"] + sum((["-map", f"{a_in}:a:{t}"] for t in selected), [])
        args += ["-c", "copy"]
        for i, t in enumerate(selected):
            tr = info.audio[t]
            if tr.title:
                args += [f"-metadata:s:a:{i}", f"title={tr.title}",
                         f"-metadata:s:a:{i}", f"handler_name={tr.title}"]
        if ext in (".mp4", ".mov"):
            args += ["-movflags", "+faststart"]
        args += tag_args(tag) + ["-y", str(part)]
        r = ff.run_ffmpeg(args, total, ctx.reporter(stage), ctx.cancel, output_path=part)
        if not r.ok:
            raise FFmpegError(f"ffmpeg failed:\n{r.stderr_tail}")
        ctx.stage("Checking nothing was lost")
        problem = verify_output(part, total, True, len(selected))
        if not problem:
            got_v = pk.read_packets(part, "video").hashes
            problem = pk.compare(pj.expected_video(), got_v, "video")
        if not problem and a_in and 0 in selected and selected[0] == 0:
            got_a = pk.read_packets(part, "audio").hashes
            problem = pk.compare(pj.expected_audio(), got_a, "audio")
        if problem:
            _reject(ctx, part, final, problem, "Merge failed the nothing-lost check")
        ctx.log("Nothing-lost check passed: every source frame is in the output exactly once.")
        _note_source_gaps(ctx, _piece_windows(pj), info.fps)
    else:
        # Encode straight from the exact packet join: one pass, frame exact.
        e = enc or EncodeSettings.from_dict(EXACT_SETTINGS.to_dict())
        from .render import _audio_codec_args, _track_meta_args
        from .encode import video_filters
        parts, amap = [], []
        vf = video_filters(e, info)
        vmap = "0:v:0"
        if vf:
            parts.append(f"[0:v:0]{','.join(vf)}[vout]")
            vmap = "[vout]"
        mix = [t for t in (audio.mix or []) if t in selected]
        out_tracks = []
        if len(mix) >= 2 and a_in:
            mix_in = []
            for t in mix:
                if audio.keep_originals:
                    parts.append(f"[{a_in}:a:{t}]asplit=2[m{t}][k{t}]")
                    mix_in.append(f"[m{t}]")
                else:
                    mix_in.append(f"[{a_in}:a:{t}]")
            parts.append(f"{''.join(mix_in)}amix=inputs={len(mix)}:normalize=0:duration=longest,"
                         f"alimiter=limit=0.98:latency=1[mixed]")
            amap.append("[mixed]")
            out_tracks.append(None)
            for t in selected:
                if t in mix and not audio.keep_originals:
                    continue
                amap.append(f"[k{t}]" if t in mix else f"{a_in}:a:{t}")
                out_tracks.append(t)
        elif a_in:
            amap = [f"{a_in}:a:{t}" for t in selected]
            out_tracks = list(selected)
        if parts:
            args += ["-filter_complex", ";".join(parts)]
        args += ["-map", vmap] + sum((["-map", m] for m in amap), [])
        args += video_codec_args(e, info, quality=q if q is not None else 18, container=ext)
        args += _audio_codec_args(e, {"out_tracks": out_tracks, "audio_filtered": bool(parts and mix),
                                      "src_codecs": [info.audio[t].codec for t in selected]}, ext)
        args += _track_meta_args(info, out_tracks, "Mixed")
        if ext in (".mp4", ".mov"):
            args += ["-movflags", "+faststart"]
        args += tag_args(tag) + ["-y", str(part)]
        r = ff.run_ffmpeg(args, total, ctx.reporter(stage), ctx.cancel, size_limit=limit, output_path=part)
        if r.status == "too_large":
            safe_unlink(part)
            _too_large_note(ctx, r, limit)
            return _packet_output(job, ctx, pj, folder, stem, ext, None, audio, srcs, tmpdir, stage, trim,
                                  NOT_COMPRESSED)
        if not r.ok:
            raise FFmpegError(f"ffmpeg failed:\n{r.stderr_tail}")
        ctx.stage("Verifying")
        problem = verify_output(part, total, True, len(out_tracks))
        if problem:
            _reject(ctx, part, final, problem)
        _verify_frames(ctx, part, total, info.fps, _piece_windows(pj), "Output")
    _finish(part, final, pj.pieces[-1].clip.path)
    ctx.log(f"Saved {final.name}")
    return final


def _log_splices_packets(ctx, pj: pk.PacketJoin):
    t = 0.0
    for p, nxt in zip(pj.pieces, pj.pieces[1:]):
        t += p.duration or 0.0
        ctx.log(f"Join at {format_hms_ms(t)}: {p.clip.path.name} -> {nxt.clip.path.name}")


def _packet_log(session, pj: pk.PacketJoin, written, trim_mode: bool) -> str:
    lines = ["Settings", "--------", "Method: exact packet matching (no estimate, no safety margin needed)", "",
             "Clip boundaries", "---------------"]
    used = {id(p.clip): p for p in pj.pieces}
    names = {id(c): n for c, n, _ in (written or [])}
    # Length each piece takes up in the output, and where it starts (merge mode).
    lengths, starts, pos = [], [], 0.0
    for p in pj.pieces:
        if trim_mode:
            length = p.video.pts[p.stop] - p.clip.info.start_time if p.stop is not None else p.clip.duration
        else:
            length = p.duration if p.duration is not None else p.clip.duration
        lengths.append(length)
        starts.append(pos)
        pos += length
    index = {id(p.clip): k for k, p in enumerate(pj.pieces)}

    def end_line(c, own_k):
        """Where the clip's original, uncut end lands in the output."""
        hit = pj.locate_end(c)
        if hit is None:
            return None
        k, t = hit
        t = min(max(t, 0.0), lengths[k])        # rounding must not push it past the output
        if not trim_mode:
            return f"    Original clip ends in merged video : {format_hms_ms(starts[k] + t)}"
        if own_k is None:                       # no file of its own: count from where it starts
            first = pj.piece_at(pj.places[id(c)][0])
            own_k = k if first is None else first
        return mg.trim_end_line(t, names.get(id(pj.pieces[k].clip), pj.pieces[k].clip.path.name),
                                k - own_k, own=own_k is not None and id(c) in index)

    pos = 0.0
    for c in session.clips:
        p = used.get(id(c))
        if p is None:
            lines += [f"{c.path.name}  (every frame also in neighbouring clips; not used)"]
            el = end_line(c, None)
            lines += ([el] if el else []) + [""]
            continue
        end = p.video.pts[p.stop] if p.stop is not None else c.duration
        length = p.duration if p.duration is not None else c.duration
        if trim_mode:
            lines += [names.get(id(c), c.path.name),
                      f"    Span kept from original clip : {format_hms_ms(0)} - {format_hms_ms(end)}"
                      + ("" if p.stop is None else "  (the rest continues in the next file)")]
        else:
            lines += [c.path.name,
                      f"    Position in merged video : {format_hms_ms(pos)} - {format_hms_ms(pos + length)}",
                      f"    Span used from original  : {format_hms_ms(0)} - {format_hms_ms(end)}"]
        el = end_line(c, index[id(c)])
        lines += ([el] if el else []) + [""]
        pos += length
    return "\n".join(lines).rstrip() + "\n"


def _write_text(path: Path, text: str):
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _fallback_session(job, ctx, session: mg.SessionPlan, out_root, merged_name, folder_name,
                      enc, audio) -> List[Path]:
    opts = job.options
    mopts = opts.merge_options()
    srcs = [c.path for c in session.clips]
    kept = session.kept
    ctx.stage("Working out overlaps")
    plan = mg.build_trim_plan(kept, mopts, ctx.log, ctx.cancel)
    first = kept[0]
    ext = output_ext(first.path, opts.container)
    outputs = []
    mark = len(ctx.warnings)

    if opts.merge_mode == "trim":
        folder = out_root / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        written = []
        for i, e in enumerate(plan):
            ctx.cancel.check()
            c = e.clip
            cut = e.trim_start
            span = Span(c.path, c.info, cut, None)
            use_enc = enc
            if enc is None and cut > 0.01:
                kf, note = (mg.pick_lossless_cut(c.path, cut, mopts.never_lose, mopts.lossless_max_repeat)
                            if opts.merge_method != "exact" else (None, "frame-exact cuts were chosen"))
                if kf is not None:
                    ctx.log(f"{c.path.name}: lossless cut at {kf:.2f}s ({note})")
                    span = Span(c.path, c.info, kf, None)
                else:
                    ctx.log(f"{c.path.name}: re-encoding for an exact cut ({note})")
                    use_enc = EncodeSettings.from_dict(EXACT_SETTINGS.to_dict())
            q, method = (resolve_quality(c.path, c.info, use_enc, ctx.cancel, ctx.log)
                         if use_enc is not None and use_enc.mode == "lossy" else (None, None))
            limit, copy_span = None, None
            if enc is not None and enc.revert_if_larger and not audio.needs_filter:
                kf = span.start if span.start <= 0.01 or opts.merge_method == "exact" else \
                    mg.pick_lossless_cut(c.path, span.start, mopts.never_lose, mopts.lossless_max_repeat)[0]
                if kf is not None and opts.merge_method != "exact":
                    copy_span = Span(c.path, c.info, kf, None)
                    plain = int(c.info.size * copy_span.length / c.info.duration) if c.info.duration else c.info.size
                    limit = _plain_limit(ctx, enc, audio, plain, q, c.path, c.info, span.length)
            extra = ""
            while True:
                if limit == "predicted":
                    span, use_enc, q, limit, extra = copy_span, None, None, None, NOT_COMPRESSED
                cut = span.start
                label = _qlabel(job.options, use_enc, q, method) if enc is not None and use_enc is not None else ""
                trim = _trim_tag(opts, [(cut, c.info.duration)] if cut > 0.01 else [])
                final = _claim(folder / f"{sanitize_filename(c.path.stem + opts.name_suffix)}{trim}{extra}{label}"
                               f"{ext}", ctx, opts, srcs)
                part = _part(final)
                ctx.progress(f"Clip {i + 1} of {len(plan)}", Progress(None, 0, None, None, None))
                tag = _merge_tag(job, c.info.trimmer_tag, audio, len(c.info.audio),
                                 enc if use_enc is not None else None, q)
                r = render([span], part, encode=use_enc, quality=q, audio=audio,
                           on_progress=ctx.reporter(f"Clip {i + 1} of {len(plan)}"), cancel=ctx.cancel,
                           log=ctx.log, tag=tag, size_limit=limit if isinstance(limit, int) else None)
                if r.status == "too_large":
                    safe_unlink(part)
                    _too_large_note(ctx, r, limit)
                    limit = "predicted"
                    continue
                break
            if not r.ok:
                raise FFmpegError(f"ffmpeg failed:\n{r.stderr_tail}")
            problem = verify_output(part, span.length, True, None)
            if problem:
                safe_unlink(part)
                raise FFmpegError(f"{c.path.name} failed verification: {problem}")
            _note_source_gaps(ctx, _span_windows([span]), c.info.fps)
            _finish(part, final, c.path)
            outputs.append(final)
            written.append((c, final.name, span.start))
            ctx.log(f"Saved {final.name}")
        if opts.generate_clip_log:
            _write_text(folder / "info.txt", mg.clip_log_for_trim(session, written, mopts)
                        + _gap_section(ctx.warnings[mark:]))
        return outputs

    # Merge into one video.
    def copy_plan():
        """Spans and cuts for a stream-copied join, or the reason there can't be one."""
        spans = [Span(e.clip.path, e.clip.info, e.trim_start, None) for e in plan]
        if opts.merge_method == "exact":
            return None, "frame-exact merge was chosen"
        reason = copy_compatible(spans)
        if reason is None:
            for s in spans:
                if s.start > 0.01:
                    # A repeat inside one continuous video is noticeable, so
                    # "auto" only stream-copies when a join repeats <= 0.5 s.
                    limit = (mopts.lossless_max_repeat if opts.merge_method == "lossless"
                             else min(0.5, mopts.lossless_max_repeat))
                    kf, note = mg.pick_lossless_cut(s.path, s.start, mopts.never_lose, limit)
                    if kf is None:
                        return None, f"{s.path.name}: {note}"
                    ctx.log(f"{s.path.name}: {note}")
                    s.start = kf
        return (spans, None) if reason is None else (None, reason)

    spans = [Span(e.clip.path, e.clip.info, e.trim_start, None) for e in plan]
    use_enc = enc
    cuts = [(e.clip, e.trim_start) for e in plan]
    extra = ""
    copied = None
    if use_enc is None:
        copied, reason = copy_plan()
        if reason is not None:
            if opts.merge_method == "lossless":
                raise FFmpegError(f"A lossless merge isn't possible here ({reason}). "
                                  f"Choose 'Auto' or 'Frame-exact' as the merge method.")
            ctx.log(f"Joining by re-encode ({reason}).")
            use_enc = EncodeSettings.from_dict(EXACT_SETTINGS.to_dict())
    q, method = (resolve_quality(first.path, first.info, use_enc, ctx.cancel, ctx.log)
                 if use_enc is not None and use_enc.mode == "lossy" else (None, None))
    limit = None
    if enc is not None and enc.revert_if_larger and not audio.needs_filter:
        copied, _reason = copy_plan()           # only worth comparing when a plain merge is possible
        if copied is not None:
            plain = int(sum(s.info.size * s.length / s.info.duration if s.info.duration else s.info.size
                            for s in copied))
            limit = _plain_limit(ctx, enc, audio, plain, q, first.path, first.info,
                                 sum(s.length for s in spans))
    while True:
        if limit == "predicted":
            use_enc, q, limit, spans, extra = None, None, None, copied, NOT_COMPRESSED
        if use_enc is None and copied is not None:
            spans = copied
            cuts = [(e.clip, s.start) for e, s in zip(plan, spans)]
        label = _qlabel(job.options, use_enc, q, method) if use_enc is not None and enc is not None else ""
        final = _claim(out_root / f"{sanitize_filename(merged_name + opts.name_suffix)}{extra}{label}{ext}",
                       ctx, opts, srcs)
        part = _part(final)
        total = sum(s.length for s in spans)
        tag = _merge_tag(job, first.info.trimmer_tag, audio, len(first.info.audio),
                         enc if use_enc is not None else None, q)
        r = render(spans, part, encode=use_enc, quality=q, audio=audio, on_progress=ctx.reporter("Merging"),
                   cancel=ctx.cancel, log=ctx.log, normalize_audio=use_enc is not None, tag=tag,
                   size_limit=limit if isinstance(limit, int) else None)
        if r.status == "too_large":
            safe_unlink(part)
            _too_large_note(ctx, r, limit)
            limit = "predicted"
            continue
        break
    if not r.ok:
        raise FFmpegError(f"ffmpeg failed:\n{r.stderr_tail}")
    ctx.stage("Verifying")
    problem = verify_output(part, total, True, None)
    if problem:
        _reject(ctx, part, final, problem, "Merge failed verification")
    if use_enc is not None:
        _verify_frames(ctx, part, total, first.info.fps, _span_windows(spans), "Merge")
    else:
        _note_source_gaps(ctx, _span_windows(spans), first.info.fps)
    _finish(part, final, kept[-1].path)
    ctx.log(f"Saved {final.name}")
    outputs.append(final)
    pos = 0.0
    for (c, t), nxt in zip(cuts, [c for c, _ in cuts[1:]]):
        pos += c.duration - t
        ctx.log(f"Join at {format_hms_ms(pos)}: {c.path.name} -> {nxt.path.name}")
    if opts.generate_clip_log:
        _write_text(final.with_name(f"{final.stem} - info.txt"), mg.clip_log_for_merge(session, cuts, mopts)
                    + _gap_section(ctx.warnings[mark:]))
    return outputs


def needs_analysis(job) -> bool:
    """True for a job set to 'Auto settings, measured' that hasn't been analysed yet."""
    return bool(job.compress and job.make_video and job.encode
                and EncodeSettings.from_dict(job.encode).needs_analysis)


def analysis_source(job) -> str:
    """The file auto settings samples: the clip, or a merge's longest clip."""
    if job.kind != "merge":
        return job.source
    def length(p):
        try:
            return ff.probe(p).duration or 0
        except (FFmpegError, OSError):
            return 0
    return max(job.clip_paths, key=length)


def analyse_job(job, cancel: CancelToken = None, log=None, on_step=None) -> dict:
    """Run the auto settings analysis for a job; returns the `chosen` dict."""
    src = analysis_source(job)
    return pick_settings(Path(src), ff.probe(src), EncodeSettings.from_dict(job.encode), cancel, log, on_step)


def _apply_auto_settings(job, ctx: JobContext):
    """A job set to auto settings, with the analysed codec and CRF filled in
    (analysing now if the queue hasn't already)."""
    if not (job.compress and job.encode):
        return job
    enc = EncodeSettings.from_dict(job.encode)
    if not enc.is_autoset:
        return job
    if not enc.chosen:
        ctx.stage("Choosing settings")
        enc.chosen = analyse_job(job, ctx.cancel, ctx.log,
                                 lambda done, total: ctx.progress("Choosing settings",
                                                                  Progress(done / total, 0, None, None, None)))
        for line in enc.chosen["report"]:
            ctx.log(line)
        job.encode = {**job.encode, "chosen": enc.chosen}   # so the queue shows what was chosen
    else:
        ctx.log(enc.chosen["report"][0] if enc.chosen.get("report") else
                f"Auto settings: {enc.chosen['codec']} CRF {enc.chosen['quality']}")
    return replace(job, encode=enc.applied().to_dict())


def run_job(job, ctx: JobContext) -> JobResult:
    """Run one job; never raises except Cancelled."""
    try:
        job = _apply_auto_settings(job, ctx)
        res = run_merge_job(job, ctx) if job.kind == "merge" else run_clip_job(job, ctx)
        res.warnings = list(dict.fromkeys(ctx.warnings))
        return res
    except Cancelled:
        raise
    except _Skip as e:
        return JobResult(status="skipped", messages=[str(e)])
    except (FFmpegError, OSError, ValueError) as e:
        ctx.log(f"FAILED: {e}")
        return JobResult(status="failed", messages=[str(e)])
