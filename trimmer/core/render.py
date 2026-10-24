"""Turning "these spans of these files" into one output file.

Every operation in the app ends up here: a single-clip trim, cutting out
the middle of a clip, a replay-session merge, a compress, an audio mix.
Two paths:

- copy:   no re-encode. Cuts snap back to keyframes (never losing
          footage) and spans are joined with ffmpeg's concat demuxer.
          Near-instant and quality untouched.
- encode: one ffmpeg run that seeks, trims, joins, scales and compresses
          in a single pass, so footage is only ever re-encoded once.
"""

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import ffmpeg as ff
from .encode import (EncodeSettings, audio_bitrate_kbps, can_copy_audio, target_video_bitrate,
                     video_codec_args, video_filters)
from .ffmpeg import CancelToken, MediaInfo, Progress, RunResult

AUDIO_RIP_FORMATS = {
    "copy": "Original (no re-encode)",
    "wav": "WAV (uncompressed)",
    "flac": "FLAC (lossless)",
    "mp3": "MP3 320 kbps",
    "m4a": "AAC 256 kbps (.m4a)",
}

_COPY_EXT = {"aac": ".m4a", "mp3": ".mp3", "opus": ".opus", "flac": ".flac", "vorbis": ".ogg",
             "alac": ".m4a", "ac3": ".ac3", "eac3": ".eac3"}


@dataclass
class Span:
    path: Path
    info: MediaInfo
    start: float = 0.0
    end: Optional[float] = None       # None = to the end of the file

    def __post_init__(self):
        self.path = Path(self.path).resolve()

    @property
    def stop(self) -> float:
        return self.info.duration if self.end is None else min(self.end, self.info.duration)

    @property
    def length(self) -> float:
        return max(0.0, self.stop - self.start)


@dataclass
class AudioPlan:
    tracks: Optional[List[int]] = None      # None = all, [] = no audio
    mix: Optional[List[int]] = None         # tracks to mix into one, or None
    volumes: Dict[int, float] = field(default_factory=dict)   # track -> gain (1.0 = unchanged)
    keep_originals: bool = True             # with mix: also keep the separate tracks after it

    def selected(self, n_available: int) -> List[int]:
        if self.tracks is None:
            return list(range(n_available))
        return [t for t in self.tracks if 0 <= t < n_available]

    @property
    def needs_filter(self) -> bool:
        return bool(self.mix) or any(abs(v - 1.0) > 1e-6 for v in self.volumes.values())


def output_ext(source: Path, container: str) -> str:
    if container in (".mp4", ".mkv"):
        return container
    ext = source.suffix.lower()
    return ext if ext in (".mp4", ".mkv", ".mov") else ".mp4"


def _common_track_count(spans: List[Span]) -> int:
    return min(len(s.info.audio) for s in spans) if spans else 0


def _max_track_count(spans: List[Span]) -> int:
    return max(len(s.info.audio) for s in spans) if spans else 0


def _track_meta_args(info: MediaInfo, out_tracks: List[Optional[int]], mixed_label: str = None) -> List[str]:
    """Carry OBS's track names (Mic, Desktop...) into the output."""
    args = []
    for i, t in enumerate(out_tracks):
        if t is None:
            if mixed_label:
                args += [f"-metadata:s:a:{i}", f"title={mixed_label}", f"-metadata:s:a:{i}", f"handler_name={mixed_label}"]
            continue
        if t < len(info.audio):
            tr = info.audio[t]
            if tr.title:
                # MP4 keeps a track's name in handler_name; MKV in title.
                args += [f"-metadata:s:a:{i}", f"title={tr.title}",
                         f"-metadata:s:a:{i}", f"handler_name={tr.title}"]
            if tr.language:
                args += [f"-metadata:s:a:{i}", f"language={tr.language}"]
    return args


def _ffconcat(spans: List[Span], tmpdir: Path, with_outpoint: bool = True) -> Path:
    lines = ["ffconcat version 1.0"]
    for s in spans:
        p = Path(s.path).resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{p}'")
        if s.start > 0.0005:
            lines.append(f"inpoint {s.start:.6f}")
        if with_outpoint and s.end is not None and s.end < s.info.duration - 0.0005:
            lines.append(f"outpoint {s.end:.6f}")
    path = tmpdir / "list.ffconcat"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def copy_compatible(spans: List[Span]) -> Optional[str]:
    """None if the spans can be joined by stream copy, else the reason."""
    sigs = {s.info.stream_signature() for s in spans}
    if len(sigs) > 1:
        return "the clips use different codecs, resolutions or audio layouts"
    return None


def snap_spans_to_keyframes(spans: List[Span], never_lose: bool = True) -> List[str]:
    """For stream copy, move each span's start back to the keyframe at or
    before it (so nothing the user wanted is lost). Mutates spans; returns notes."""
    notes = []
    for s in spans:
        if s.start <= 0.0005:
            continue
        kf = ff.keyframe_at_or_before(s.path, s.start)
        if kf is None:
            kf = 0.0
        if s.start - kf > 0.01:
            notes.append(f"{s.path.name}: start moved back {s.start - kf:.2f}s to a keyframe "
                         f"({s.start:.2f}s -> {kf:.2f}s) so nothing is lost")
        s.start = kf
    return notes


def render(spans: List[Span], out_path: Path, *, encode: Optional[EncodeSettings], quality: Optional[int],
           audio: AudioPlan, include_video: bool = True, mixed_label: str = "Mixed",
           on_progress: Callable[[Progress], None] = None, cancel: CancelToken = None,
           size_limit: int = None, log: Callable[[str], None] = None,
           normalize_audio: bool = False) -> RunResult:
    """Write `spans` (joined in order) to out_path.

    encode=None -> stream copy (spans must already be keyframe-snapped and
    stream-compatible; audio mix/volume still works, re-encoding audio only).
    """
    log = log or (lambda m: None)
    out_path = Path(out_path)
    total = sum(s.length for s in spans)
    first = spans[0].info
    container = out_path.suffix.lower()

    if encode is None:
        return _render_copy(spans, out_path, audio, include_video, mixed_label, total,
                            on_progress, cancel, container)

    if encode.mode == "target_size":
        return _render_two_pass(spans, out_path, encode, audio, include_video, mixed_label, total,
                                on_progress, cancel, container, log, normalize_audio)

    args, maps_meta = _encode_inputs_and_filters(spans, encode, audio, include_video, normalize_audio)
    args += maps_meta["maps"]
    if include_video and first.has_video:
        args += video_codec_args(encode, first, quality=quality, container=container)
        if maps_meta["vf"]:
            args += ["-vf", ",".join(maps_meta["vf"])]
    args += _audio_codec_args(encode, maps_meta, container)
    args += _track_meta_args(first, maps_meta["out_tracks"], mixed_label)
    args += _container_args(container) + ["-y", str(out_path)]
    return ff.run_ffmpeg(args, total, on_progress, cancel, size_limit=size_limit, output_path=out_path)


def _container_args(container: str) -> List[str]:
    a = ["-map_metadata", "-1", "-map_chapters", "-1"]
    if container in (".mp4", ".mov"):
        a += ["-movflags", "+faststart"]
    return a


def _audio_codec_args(encode: EncodeSettings, meta, container: str) -> List[str]:
    if not meta["out_tracks"]:
        return []
    if meta["audio_filtered"] or encode.audio != "copy" or encode.mode == "target_size":
        kbps = audio_bitrate_kbps(encode, default=192)
        return ["-c:a", "aac", "-b:a", f"{kbps}k"]
    if all(can_copy_audio(c, container) for c in meta["src_codecs"]):
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "256k"]


def _encode_inputs_and_filters(spans: List[Span], encode: EncodeSettings, audio: AudioPlan,
                               include_video: bool, normalize_audio: bool):
    """Inputs are seeked with -ss/-t per span (fast and frame-accurate when
    re-encoding). One span with no audio processing maps streams directly
    (so audio can still be copied); anything else goes through one
    filter_complex that trims, joins and mixes."""
    first = spans[0].info
    args = []
    for s in spans:
        if s.start > 0.0005:
            args += ["-ss", f"{s.start:.3f}"]
        if s.end is not None and s.end < s.info.duration - 0.0005:
            args += ["-t", f"{s.length:.3f}"]
        args += ["-i", str(s.path)]

    n_avail = _common_track_count(spans)
    if _max_track_count(spans) > n_avail:
        n_avail = _max_track_count(spans)   # missing tracks are filled with silence below
    selected = audio.selected(n_avail)
    vf = video_filters(encode, first) if include_video else []
    video = include_video and all(s.info.has_video for s in spans)

    simple = len(spans) == 1 and not audio.needs_filter and not normalize_audio
    if simple:
        maps = (["-map", "0:v:0"] if video else []) + sum((["-map", f"0:a:{t}"] for t in selected), [])
        return args, {"maps": maps, "vf": vf, "out_tracks": selected, "audio_filtered": False,
                      "src_codecs": [first.audio[t].codec for t in selected]}

    parts = []
    n = len(spans)
    w, h = first.width, first.height
    sizes_differ = video and any((s.info.width, s.info.height) != (w, h) for s in spans)
    for i, s in enumerate(spans):
        if video:
            chain = "setpts=PTS-STARTPTS"
            if sizes_differ:
                chain += (f",scale={w}:{h}:force_original_aspect_ratio=decrease,"
                          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
            parts.append(f"[{i}:v:0]{chain}[v{i}]")
        for t in selected:
            if t < len(s.info.audio):
                parts.append(f"[{i}:a:{t}]asetpts=PTS-STARTPTS,"
                             f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{i}t{t}]")
            else:
                # This clip lacks the track: fill with silence so the others survive.
                parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={s.length:.3f},"
                             f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{i}t{t}]")
    concat_in = "".join((f"[v{i}]" if video else "") + "".join(f"[a{i}t{t}]" for t in selected)
                        for i in range(n))
    outs = ("[cv]" if video else "") + "".join(f"[c{t}]" for t in selected)
    parts.append(f"{concat_in}concat=n={n}:v={1 if video else 0}:a={len(selected)}{outs}")

    labels = {t: f"[c{t}]" for t in selected}
    for t in selected:
        gain = audio.volumes.get(t, 1.0)
        if abs(gain - 1.0) > 1e-6:
            parts.append(f"[c{t}]volume={gain:.3f}[g{t}]")
            labels[t] = f"[g{t}]"
    out_tracks: List[Optional[int]] = []
    final_audio_labels = []
    mix = [t for t in (audio.mix or []) if t in selected]
    if len(mix) >= 2:
        # Each mixed track may also be kept separately, so split it first.
        mix_in = []
        for t in mix:
            if audio.keep_originals:
                parts.append(f"{labels[t]}asplit=2[m{t}][k{t}]")
                mix_in.append(f"[m{t}]")
                labels[t] = f"[k{t}]"
            else:
                mix_in.append(labels[t])
        parts.append(f"{''.join(mix_in)}amix=inputs={len(mix)}:normalize=0:duration=longest,"
                     f"alimiter=limit=0.98[mixed]")
        final_audio_labels.append("[mixed]")
        out_tracks.append(None)
        for t in selected:
            if t in mix and not audio.keep_originals:
                continue
            final_audio_labels.append(labels[t])
            out_tracks.append(t)
    else:
        for t in selected:
            final_audio_labels.append(labels[t])
            out_tracks.append(t)

    maps = []
    if video:
        if vf:
            parts.append(f"[cv]{','.join(vf)}[vout]")
            maps += ["-map", "[vout]"]
        else:
            maps += ["-map", "[cv]"]
    for lbl in final_audio_labels:
        maps += ["-map", lbl]
    args += ["-filter_complex", ";".join(parts)]
    return args, {"maps": maps, "vf": [], "out_tracks": out_tracks, "audio_filtered": True,
                  "src_codecs": []}


def copy_audio_maps(first: MediaInfo, audio: AudioPlan, selected: List[int], container: str,
                    input_index: int = 0):
    """Audio for a stream-copy output: (filter parts, maps, output tracks,
    audio codec args). Tracks are copied untouched unless a mix or volume
    change needs them re-encoded."""
    src = f"{input_index}:a"
    out_tracks: List[Optional[int]] = []
    maps, parts = [], []
    mix = [t for t in (audio.mix or []) if t in selected]
    if not audio.needs_filter:
        for t in selected:
            maps += ["-map", f"{src}:{t}"]
            out_tracks.append(t)
        ok = all(can_copy_audio(first.audio[t].codec, container) for t in selected)
        return parts, maps, out_tracks, (["-c:a", "copy"] if ok else ["-c:a", "aac", "-b:a", "256k"])
    labels = {}
    for t in selected:
        gain = audio.volumes.get(t, 1.0)
        labels[t] = f"[{src}:{t}]"
        if abs(gain - 1.0) > 1e-6:
            parts.append(f"[{src}:{t}]volume={gain:.3f}[g{t}]")
            labels[t] = f"[g{t}]"
    if len(mix) >= 2:
        mix_in = []
        for t in mix:
            if audio.keep_originals:
                parts.append(f"{labels[t]}asplit=2[m{t}][k{t}]")
                mix_in.append(f"[m{t}]")
                labels[t] = f"[k{t}]"
            else:
                mix_in.append(labels[t])
        parts.append(f"{''.join(mix_in)}amix=inputs={len(mix)}:normalize=0:duration=longest,"
                     f"alimiter=limit=0.98[mixed]")
        maps += ["-map", "[mixed]"]
        out_tracks.append(None)
    for t in selected:
        if t in mix and len(mix) >= 2 and not audio.keep_originals:
            continue
        maps += ["-map", labels[t]]
        out_tracks.append(t)
    return parts, maps, out_tracks, ["-c:a", "aac", "-b:a", "192k"]


def _render_copy(spans, out_path, audio: AudioPlan, include_video, mixed_label, total,
                 on_progress, cancel, container) -> RunResult:
    first = spans[0].info
    n_avail = _common_track_count(spans)
    selected = audio.selected(n_avail)
    video = include_video and first.has_video
    with tempfile.TemporaryDirectory(prefix="trimmer_copy_") as td:
        if len(spans) == 1:
            s = spans[0]
            args = []
            if s.start > 0.0005:
                args += ["-ss", f"{s.start:.6f}"]
            if s.end is not None and s.end < s.info.duration - 0.0005:
                args += ["-t", f"{s.length:.6f}"]
            args += ["-i", str(s.path)]
        else:
            args = ["-f", "concat", "-safe", "0", "-i", str(_ffconcat(spans, Path(td)))]

        maps = ["-map", "0:v:0"] if video else []
        parts, amaps, out_tracks, acodec = copy_audio_maps(first, audio, selected, container)
        if parts:
            args += ["-filter_complex", ";".join(parts)]
        maps += amaps
        codec = (["-c:v", "copy"] if video else []) + acodec
        args += maps + codec + _track_meta_args(first, out_tracks, mixed_label)
        args += ["-avoid_negative_ts", "make_zero"] + _container_args(container) + ["-y", str(out_path)]
        return ff.run_ffmpeg(args, total, on_progress, cancel, output_path=out_path)


def _render_two_pass(spans, out_path, encode, audio, include_video, mixed_label, total,
                     on_progress, cancel, container, log, normalize_audio) -> RunResult:
    first = spans[0].info
    args, meta = _encode_inputs_and_filters(spans, encode, audio, include_video, normalize_audio)
    n_audio = len(meta["out_tracks"])
    audio_kbps = audio_bitrate_kbps(encode, default=128) * n_audio
    vkbps = target_video_bitrate(encode, total, audio_kbps)
    log(f"Target {encode.target_size_mb:g} MB over {total:.1f}s: video {vkbps} kbps, audio {audio_kbps} kbps total")
    enc_args = _audio_codec_args(encode, meta, container)
    if encode.audio == "copy" and n_audio:
        enc_args = ["-c:a", "aac", "-b:a", "128k"]
    two_pass = encode.encoder_name in ("libx264", "libx265")
    vf = ["-vf", ",".join(meta["vf"])] if meta["vf"] else []

    def half(p):
        return (lambda pr: on_progress(Progress(
            None if pr.fraction is None else (pr.fraction / 2 + (0.5 if p == 2 else 0)),
            pr.out_time, pr.size, pr.speed, None))) if on_progress else None

    with tempfile.TemporaryDirectory(prefix="trimmer_2pass_") as td:
        if two_pass:
            # Run inside the temp folder with a bare relative stats name, so
            # x265's ':'-separated params never see a Windows drive letter.
            # Pass 1 maps the same streams as pass 2 (a filter graph with
            # unmapped outputs is an error); the audio work is thrown away.
            p1 = args + meta["maps"] + video_codec_args(encode, first, bitrate_kbps=vkbps, pass_no=1,
                                                        passlog="pass", container=container)
            p1 += vf + (["-c:a", "aac", "-b:a", "64k"] if n_audio else []) + ["-f", "null", "-"]
            r = ff.run_ffmpeg(p1, total, half(1), cancel, cwd=td)
            if not r.ok:
                return r
        p2 = args + meta["maps"] + video_codec_args(
            encode, first, bitrate_kbps=vkbps, pass_no=2 if two_pass else None,
            passlog="pass", container=container) + vf + enc_args
        p2 += _track_meta_args(first, meta["out_tracks"], mixed_label)
        p2 += _container_args(container) + ["-y", str(Path(out_path).resolve())]
        return ff.run_ffmpeg(p2, total, half(2) if two_pass else on_progress, cancel,
                             output_path=Path(out_path).resolve(), cwd=td)


def rip_audio(spans: List[Span], track: int, fmt: str, out_base: Path,
              on_progress=None, cancel: CancelToken = None) -> (RunResult, Path):
    """Extract one audio track (over the given spans) to its own file.
    Returns (result, actual output path)."""
    first = spans[0].info
    tr = first.audio[track]
    total = sum(s.length for s in spans)
    multi = len(spans) > 1
    if fmt == "copy":
        ext = _COPY_EXT.get(tr.codec, ".mka")
        codec = ["-c:a", "copy"]
    elif fmt == "wav":
        ext, codec = ".wav", ["-c:a", "pcm_s16le"]
    elif fmt == "flac":
        ext, codec = ".flac", ["-c:a", "flac"]
    elif fmt == "mp3":
        ext, codec = ".mp3", ["-c:a", "libmp3lame", "-b:a", "320k"]
    else:
        ext, codec = ".m4a", ["-c:a", "aac", "-b:a", "256k"]
    out_path = Path(str(out_base) + ext)
    inputs = []
    for s in spans:
        if s.start > 0.0005:
            inputs += ["-ss", f"{s.start:.6f}"]
        if s.end is not None and s.end < s.info.duration - 0.0005:
            inputs += ["-t", f"{s.length:.6f}"]
        inputs += ["-i", str(s.path)]
    if multi:
        # Several parts: join with the concat filter (sample-exact; the
        # concat demuxer would let in extra audio before each in-point).
        graph = "".join(f"[{i}:a:{track}]" for i in range(len(spans))) + f"concat=n={len(spans)}:v=0:a=1[a]"
        args = inputs + ["-filter_complex", graph, "-map", "[a]"]
    else:
        args = inputs + ["-map", f"0:a:{track}"]
    args += ["-vn"] + codec + ["-map_metadata", "-1"]
    if tr.title:
        args += ["-metadata", f"title={tr.title}"]
    if ext in (".m4a",):
        args += ["-movflags", "+faststart"]
    args += ["-y", str(out_path)]
    return ff.run_ffmpeg(args, total, on_progress, cancel, output_path=out_path), out_path


def verify_output(path: Path, expected_duration: float, expect_video: bool = True,
                  n_audio: int = None) -> Optional[str]:
    """None if the output looks complete, else a reason. Used before any
    original is deleted."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return "output file is missing or empty"
    try:
        info = ff.probe(path, use_cache=False)
    except ff.FFmpegError as e:
        return f"output can't be read back: {e}"
    if expect_video and not info.has_video:
        return "output has no video stream"
    if n_audio is not None and len(info.audio) != n_audio:
        return f"output has {len(info.audio)} audio track(s), expected {n_audio}"
    tolerance = max(1.0, expected_duration * 0.02)
    if expected_duration > 0 and abs(info.duration - expected_duration) > tolerance:
        return (f"output is {info.duration:.1f}s long but {expected_duration:.1f}s was expected")
    return None
