"""Smart cut: frame-exact trims that keep the original video untouched.

A stream copy can only start on a keyframe. Smart cut re-encodes just the
frames from the cut up to the next keyframe (normally under two seconds)
and stream-copies everything from that keyframe on, so the cut lands on
the exact frame while nearly all of the clip keeps its original quality.

How the pieces fit together:
- Each piece is written as MPEG-TS with its parameter sets (SPS/PPS)
  in-band, so a decoder switches from the re-encoded head's settings to
  OBS's settings at the keyframe where the copied part begins.
- For H.264 the head uses a different SPS/PPS id, so the two sets never
  overwrite each other.
- Audio is cut packet by packet (no re-encode) and joined separately.

Every result is checked by decoding across each seam and comparing the
pictures with the source. If anything differs (an encoder OBS used that
can't be matched, open-GOP keyframes, an unusual codec) the caller falls
back to re-encoding the whole trim.
"""

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from . import ffmpeg as ff
from .ffmpeg import CancelToken, FFmpegError, Progress, RunResult
from .render import AudioPlan, Span, _container_args, _track_meta_args, copy_audio_maps
from .util import safe_unlink

SUPPORTED = {"h264": "libx264", "hevc": "libx265"}
_ANNEXB = {"h264": "h264_mp4toannexb", "hevc": "hevc_mp4toannexb"}
EPS = 0.0015          # a start this close to a keyframe counts as on it
CHECK_SECONDS = 1.5   # how much of each copied part is compared after a seam


class SmartCutError(FFmpegError):
    """Smart cut isn't possible or didn't verify; re-encode instead."""


@dataclass
class VideoProps:
    codec: str = ""
    profile: str = ""
    level: int = 0
    pix_fmt: str = ""
    tag: str = ""
    color_range: str = ""
    color_space: str = ""
    color_primaries: str = ""
    color_transfer: str = ""


def video_props(path: Path) -> VideoProps:
    out = ff.run_quiet([ff.ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,profile,level,pix_fmt,codec_tag_string,color_range,"
                        "color_space,color_primaries,color_transfer", "-of", "json", str(path)])
    try:
        s = (json.loads(out.stdout).get("streams") or [{}])[0]
    except (json.JSONDecodeError, IndexError):
        s = {}
    return VideoProps(codec=s.get("codec_name", ""), profile=s.get("profile", ""),
                      level=int(s.get("level") or 0), pix_fmt=s.get("pix_fmt", ""),
                      tag=s.get("codec_tag_string", ""), color_range=s.get("color_range", ""),
                      color_space=s.get("color_space", ""), color_primaries=s.get("color_primaries", ""),
                      color_transfer=s.get("color_transfer", ""))


def unsupported_reason(info, props: VideoProps) -> Optional[str]:
    if props.codec not in SUPPORTED:
        return f"smart cut supports H.264 and H.265 only (this clip is {props.codec or 'unknown'})"
    if not ff.has_encoder(SUPPORTED[props.codec]):
        return f"this ffmpeg has no {SUPPORTED[props.codec]} encoder"
    return None


def _x264_profile(p: str) -> Optional[str]:
    p = p.lower()
    if "baseline" in p:
        return "baseline"
    if "4:4:4" in p:
        return "high444"
    if "4:2:2" in p:
        return "high422"
    if "high 10" in p:
        return "high10"
    if p.startswith("main"):
        return "main"
    if p.startswith("high"):
        return "high"
    return None


def head_codec_args(props: VideoProps) -> List[str]:
    """Encoder settings for the re-encoded head, matched to the source so
    the join stays smooth. Quality is set high since it's only a second or so."""
    args = ["-pix_fmt", props.pix_fmt] if props.pix_fmt else []
    if props.codec == "h264":
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "16",
                 "-x264-params", "sps-id=1:repeat-headers=1"]
        prof = _x264_profile(props.profile)
        if prof:
            args += ["-profile:v", prof]
        if props.level:
            args += ["-level:v", f"{props.level / 10:.1f}"]
    else:
        args += ["-c:v", "libx265", "-preset", "medium",
                 "-x265-params", "log-level=error:crf=18:open-gop=0:repeat-headers=1"]
    for opt, val in (("-color_range", props.color_range), ("-colorspace", props.color_space),
                     ("-color_primaries", props.color_primaries), ("-color_trc", props.color_transfer)):
        if val and val != "unknown":
            args += [opt, val]
    return args


def _packets(path: Path, a: float, b: Optional[float]) -> List[tuple]:
    """(presentation time, is keyframe) for video packets in roughly [a, b],
    sorted by time."""
    end = f"{b + 3:.3f}" if b is not None else ""
    out = ff.run_quiet([ff.ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
                        "-read_intervals", f"{max(0.0, a - 3):.3f}%{end}",
                        "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path)])
    rows = []
    for line in out.stdout.splitlines():
        parts = line.strip().split(",")
        try:
            rows.append((float(parts[0]), "K" in parts[1]))
        except (ValueError, IndexError):
            pass
    return sorted(rows)


@dataclass
class Piece:
    kind: str                  # "encode" | "copy"
    start: float               # source time of the piece's first frame
    end: float                 # source time just after its last frame
    frames: int                # frames in the piece


def plan_span(span: Span) -> List[Piece]:
    """Split a span into: re-encoded frames from the cut to the first
    keyframe, an untouched copy from there to the last keyframe before the
    end, and re-encoded frames from that keyframe to the end. A span that
    starts or ends on a keyframe (or the end of the file) skips that part."""
    s, stop = span.start, span.stop
    to_eof = span.end is None
    rows = _packets(span.path, s, None if to_eof else stop)
    frames = [t for t, _ in rows if s - EPS <= t and (to_eof or t < stop - EPS)]
    if not frames:
        raise SmartCutError("couldn't find the frames at the cut")
    keys = [t for t, k in rows if k and frames[0] <= t <= frames[-1]]
    a = keys[0] if keys else None                  # where copying can start
    b = frames[-1] + 1.0 if to_eof else (keys[-1] if keys else None)   # where it must stop
    if to_eof:
        b = float("inf")

    def count(x, y):
        return sum(1 for t in frames if x <= t < y)

    def piece(kind, x, y):
        inside = [t for t in frames if x <= t < y]
        return Piece(kind, inside[0], y if y != float("inf") else stop, len(inside))

    if a is None or b is None or a >= b or count(a, b) == 0:
        return [piece("encode", frames[0], float("inf"))]
    pieces = []
    if count(frames[0], a):
        pieces.append(piece("encode", frames[0], a))
    pieces.append(piece("copy", a, b))
    if b != float("inf") and count(b, float("inf")):
        pieces.append(piece("encode", b, float("inf")))
    return pieces


def _framemd5(args: List[str], cancel: CancelToken) -> List[str]:
    out = ff.run_quiet([ff.ffmpeg_bin(), "-hide_banner", "-v", "error"] + args
                       + ["-map", "0:v:0", "-fps_mode", "passthrough", "-f", "framemd5", "-"])
    if cancel is not None:
        cancel.check()
    errors = out.stderr.strip()
    if out.returncode != 0 or errors:
        raise SmartCutError(f"the joined video doesn't decode cleanly ({errors[-200:] or 'ffmpeg failed'})")
    return [ln.rsplit(",", 1)[-1].strip() for ln in out.stdout.splitlines()
            if ln.strip() and not ln.startswith("#")]


def _contains(hay: List[str], needle: List[str]) -> bool:
    n = len(needle)
    return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def verify_seams(out: Path, src: Path, seams: List[tuple], cancel: CancelToken = None) -> Optional[str]:
    """seams: (output time, source time, "in" | "out"). "in" is where copied
    footage starts after re-encoded frames, "out" where it ends before
    them. Decodes the output across each seam and checks the copied side
    shows exactly the source's pictures. Returns a problem, or None."""
    w = CHECK_SECONDS
    for out_t, src_t, side in seams:
        if side == "in":
            # Start decoding at the re-encoded part before the seam (it's
            # short and starts on its own keyframe).
            got = _framemd5(["-ss", f"{max(0.0, out_t[0]):.6f}", "-i", str(out),
                             "-t", f"{out_t[1] + w + 0.5:.6f}"], cancel)
            want = _framemd5(["-ss", f"{src_t:.6f}", "-i", str(src), "-t", f"{w:.6f}"], cancel)
            want = want[:-2] if len(want) > 4 else want     # rounding at the window's end
        else:
            got = _framemd5(["-ss", f"{max(0.0, out_t - w - 0.5):.6f}", "-i", str(out),
                             "-t", f"{w + 1.5:.6f}"], cancel)
            want = _framemd5(["-ss", f"{max(0.0, src_t - w):.6f}", "-i", str(src),
                              "-t", f"{w:.6f}"], cancel)
            want = want[2:-1] if len(want) > 4 else want
        if not want or not _contains(got, want):
            return f"the pictures at the join near {src_t:.2f}s don't match the original"
    return None


def count_frames(path: Path) -> int:
    out = ff.run_quiet([ff.ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-count_packets",
                        "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)])
    try:
        return int(out.stdout.strip().strip(","))
    except ValueError:
        return -1


def smart_render(spans: List[Span], out_path: Path, audio: AudioPlan, *, mixed_label: str = "Mixed",
                 on_progress: Callable[[Progress], None] = None, cancel: CancelToken = None,
                 log: Callable[[str], None] = None) -> RunResult:
    """Write the spans (all from one source) to out_path by smart cut.
    Raises SmartCutError when smart cut can't be used or didn't verify."""
    log = log or (lambda m: None)
    out_path = Path(out_path)
    src = spans[0].path
    info = spans[0].info
    props = video_props(src)
    reason = unsupported_reason(info, props)
    if reason:
        raise SmartCutError(reason)
    container = out_path.suffix.lower()
    total = sum(s.length for s in spans)
    bsf = _ANNEXB[props.codec]

    plans = [plan_span(s) for s in spans]
    encoded = sum(p.end - p.start for pl in plans for p in pl if p.kind == "encode")
    cuts = sum(1 for pl in plans for p in pl if p.kind == "encode")
    log(f"Smart cut: re-encoding {encoded:.2f}s at {cuts} cut(s), copying the other "
        f"{max(0.0, total - encoded):.2f}s untouched")

    def report(done: float, length: float):
        if not on_progress:
            return None
        return lambda pr: on_progress(Progress(
            min(1.0, (done + (pr.fraction or 0) * length) / total) if total else None,
            done + pr.out_time, pr.size, pr.speed, None))

    with tempfile.TemporaryDirectory(prefix="trimmer_smart_") as td:
        td = Path(td)
        vlist, alist = ["ffconcat version 1.0"], ["ffconcat version 1.0"]
        seams, out_t, done, idx, n_frames = [], 0.0, 0.0, 0, 0
        selected = audio.selected(len(info.audio))
        for span, plan in zip(spans, plans):
            prev = None
            for p in plan:
                idx += 1
                piece = td / f"v{idx}.ts"
                length = max(0.0, p.end - p.start)
                # Re-encoding: seek a hair before the first frame so rounding can't
                # skip it. Copying: seek to the keyframe itself (a copy starts on
                # the keyframe at or before the seek point).
                at = p.start - 0.0005 if p.kind == "encode" else p.start
                seek = ["-ss", f"{at:.6f}"] if p.start > 0.0005 else []
                args = seek + ["-i", str(src), "-map", "0:v:0", "-frames:v", str(p.frames),
                               "-an", "-sn", "-dn"]
                if p.kind == "encode":
                    args += ["-fps_mode", "passthrough"] + head_codec_args(props)
                else:
                    args += ["-c:v", "copy"]
                args += ["-bsf:v", bsf, "-f", "mpegts", "-y", str(piece)]
                r = ff.run_ffmpeg(args, length, report(done, length), cancel, output_path=piece)
                if not r.ok:
                    raise SmartCutError(f"writing a piece failed: {r.stderr_tail[-300:]}")
                vlist.append(f"file '{piece.as_posix()}'")
                if prev is not None and prev.kind == "encode" and p.kind == "copy":
                    seams.append(((out_t - (prev.end - prev.start), prev.end - prev.start), p.start, "in"))
                if prev is not None and prev.kind == "copy" and p.kind == "encode":
                    seams.append((out_t, p.start, "out"))
                out_t += length
                done += length
                n_frames += p.frames
                prev = p
            if selected:
                apiece = td / f"a{idx}.mka"
                coarse = max(0.0, span.start - 10.0)
                args = (["-ss", f"{coarse:.6f}"] if coarse > 0 else []) + ["-i", str(src)]
                args += ["-ss", f"{span.start - coarse:.6f}"]
                if span.end is not None:
                    args += ["-t", f"{span.length:.6f}"]
                args += ["-map", "0:a"]
                args += ["-c:a", "copy", "-vn", "-y", str(apiece)]
                r = ff.run_ffmpeg(args, span.length, None, cancel, output_path=apiece)
                if not r.ok:
                    raise SmartCutError(f"cutting the audio failed: {r.stderr_tail[-300:]}")
                alist.append(f"file '{apiece.as_posix()}'")

        (td / "video.ffconcat").write_text("\n".join(vlist) + "\n", encoding="utf-8")
        args = ["-f", "concat", "-safe", "0", "-i", str(td / "video.ffconcat")]
        out_tracks: List[Optional[int]] = []
        maps = ["-map", "0:v:0"]
        codec = ["-c:v", "copy"]
        if selected:
            (td / "audio.ffconcat").write_text("\n".join(alist) + "\n", encoding="utf-8")
            args += ["-f", "concat", "-safe", "0", "-i", str(td / "audio.ffconcat")]
            fparts, amaps, out_tracks, acodec = copy_audio_maps(info, audio, selected, container,
                                                                input_index=1)
            maps += amaps
            codec += acodec
            if fparts:
                args += ["-filter_complex", ";".join(fparts)]
        if props.tag in ("hvc1", "hev1", "avc1", "avc3") and container in (".mp4", ".mov"):
            codec += ["-tag:v", props.tag]
        args += maps + codec + _track_meta_args(info, out_tracks, mixed_label)
        args += _container_args(container) + ["-y", str(out_path)]
        r = ff.run_ffmpeg(args, total, None, cancel, output_path=out_path)
        if not r.ok:
            return r

    got = count_frames(out_path)
    problem = (None if got == n_frames else f"the output has {got} frames, {n_frames} were expected") \
        or verify_seams(out_path, src, seams, cancel)
    if problem:
        safe_unlink(out_path)
        raise SmartCutError(problem)
    return r
