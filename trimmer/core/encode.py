"""Encoding settings, ffmpeg argument building and automatic CRF selection.

Ported from compress_videos.py. Behaviour kept: lossy/lossless modes, CRF
or auto2/auto3 per-file CRF, presets, audio copy by default, labels showing
the CRF actually used, revert-if-larger. Fixed: the libvmaf crash on Windows
paths (handoff 7.1) and auto3 returning a too-high CRF when no trial met the
target (7.2). New: hardware encoders, AV1, target file size, resolution and
fps caps, keyframe interval.
"""

import json
import math
import re
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import ffmpeg as ff
from .ffmpeg import CancelToken, FFmpegError, MediaInfo

VALID_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow"]

CODECS = {"h264": "H264", "h265": "H265", "av1": "AV1"}
ENCODERS = {"software": "Software (CPU)", "nvenc": "NVIDIA NVENC",
            "qsv": "Intel Quick Sync", "amf": "AMD AMF"}

_ENCODER_NAMES = {
    ("software", "h264"): "libx264", ("software", "h265"): "libx265", ("software", "av1"): "libsvtav1",
    ("nvenc", "h264"): "h264_nvenc", ("nvenc", "h265"): "hevc_nvenc", ("nvenc", "av1"): "av1_nvenc",
    ("qsv", "h264"): "h264_qsv", ("qsv", "h265"): "hevc_qsv", ("qsv", "av1"): "av1_qsv",
    ("amf", "h264"): "h264_amf", ("amf", "h265"): "hevc_amf", ("amf", "av1"): "av1_amf",
}

_NVENC_PRESET = {"ultrafast": "p1", "superfast": "p1", "veryfast": "p2", "faster": "p3",
                 "fast": "p4", "medium": "p5", "slow": "p6", "slower": "p7", "veryslow": "p7"}
_SVT_PRESET = {"ultrafast": 12, "superfast": 11, "veryfast": 10, "faster": 9, "fast": 8,
               "medium": 7, "slow": 5, "slower": 4, "veryslow": 3}
_QSV_PRESET = {"ultrafast": "veryfast", "superfast": "veryfast"}
_AMF_QUALITY = {"ultrafast": "speed", "superfast": "speed", "veryfast": "speed", "faster": "speed",
                "fast": "balanced", "medium": "balanced", "slow": "quality", "slower": "quality",
                "veryslow": "quality"}

# Audio codecs that can't be stream-copied into an MP4.
_MP4_UNSAFE_AUDIO = re.compile(r"^(pcm_.*|vorbis|wmav.*|ra_.*|cook)$")


@dataclass
class EncodeSettings:
    mode: str = "lossy"               # lossy | lossless | target_size
    codec: str = "h265"               # h264 | h265 | av1
    encoder: str = "software"         # software | nvenc | qsv | amf
    quality: object = 23              # int CRF/CQ, or "auto2" / "auto3"
    preset: str = "medium"
    target_size_mb: float = 25.0
    audio: str = "copy"               # "copy" or a kbps number as text, e.g. "192"
    max_height: int = 0               # 0 = keep
    max_fps: float = 0                # 0 = keep
    keyframe_interval: float = 0      # seconds, 0 = encoder default
    auto3_target: float = 93.0        # VMAF target (SSIM fallback uses 97)
    revert_if_larger: bool = False

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        s = cls(**known)
        s.validate()
        return s

    def validate(self):
        if self.mode not in ("lossy", "lossless", "target_size"):
            self.mode = "lossy"
        if self.codec not in CODECS:
            self.codec = "h265"
        if self.encoder not in ENCODERS:
            self.encoder = "software"
        if self.preset not in VALID_PRESETS:
            self.preset = "medium"
        if isinstance(self.quality, str) and self.quality not in ("auto2", "auto3"):
            try:
                self.quality = int(self.quality)
            except ValueError:
                self.quality = 23
        if self.audio != "copy":
            try:
                self.audio = str(int(self.audio))
            except (TypeError, ValueError):
                self.audio = "copy"
        try:
            self.auto3_target = min(99.0, max(80.0, float(self.auto3_target)))
        except (TypeError, ValueError):
            self.auto3_target = 93.0
        return self

    @property
    def is_auto(self) -> bool:
        return self.mode == "lossy" and self.quality in ("auto2", "auto3")

    @property
    def encoder_name(self) -> str:
        enc = self.encoder
        if self.mode == "lossless":
            enc = "software"      # hardware encoders have no reliable lossless mode
        return _ENCODER_NAMES[(enc, self.codec)]

    def problems(self) -> List[str]:
        """Settings that can't work, as plain-language messages."""
        out = []
        if self.mode == "lossless" and self.codec == "av1":
            out.append("AV1 (SVT-AV1) has no lossless mode. Pick H.264 or H.265 for lossless.")
        if self.mode == "target_size" and self.target_size_mb <= 0:
            out.append("Target size must be more than 0 MB.")
        name = self.encoder_name
        if not ff.has_encoder(name):
            out.append(f"Your ffmpeg build doesn't include the {name} encoder.")
        return out


def crf_range(codec: str) -> Tuple[int, int]:
    return (0, 63) if codec == "av1" else (0, 51)


def quality_label(settings: EncodeSettings, quality: int, method: str = None) -> str:
    """Text appended to output names, e.g. ' (LOSSY), (CRF23, H265, medium)'.
    Same format as the old compressor so existing outputs still make sense."""
    codec = CODECS[settings.codec]
    if settings.mode == "lossless":
        return " (LOSSLESS)"
    hw = settings.encoder != "software"
    hw_tag = f" {settings.encoder.upper()}" if hw else ""
    if settings.mode == "target_size":
        return f" (TARGET {settings.target_size_mb:g}MB), ({codec}{hw_tag}, {settings.preset})"
    word = "CQ" if hw else "CRF"
    tag = f" {method}" if method else ""
    return f" (LOSSY), ({word}{quality}{tag}, {codec}{hw_tag}, {settings.preset})"


def video_filters(settings: EncodeSettings, info: MediaInfo) -> List[str]:
    f = []
    if settings.max_height and info.height and info.height > settings.max_height:
        f.append(f"scale=-2:{int(settings.max_height)}:flags=lanczos")
    if settings.max_fps and info.fps and info.fps > settings.max_fps + 0.5:
        f.append(f"fps={settings.max_fps:g}")
    return f


def output_fps(settings: EncodeSettings, info: MediaInfo) -> float:
    fps = info.fps or 30.0
    if settings.max_fps and fps > settings.max_fps + 0.5:
        fps = settings.max_fps
    return fps


def video_codec_args(settings: EncodeSettings, info: MediaInfo, quality: Optional[int] = None,
                     bitrate_kbps: Optional[int] = None, pass_no: Optional[int] = None,
                     passlog: str = None, container: str = ".mp4") -> List[str]:
    """-c:v and its quality/speed options. `quality` is the resolved CRF/CQ
    for lossy mode; `bitrate_kbps` is used for target-size mode."""
    name = settings.encoder_name
    args = ["-c:v", name]
    preset = settings.preset
    x265_params = []

    if name in ("libx264", "libx265"):
        args += ["-preset", preset]
        if settings.mode == "lossless":
            if name == "libx264":
                args += ["-qp", "0"]
            else:
                x265_params.append("lossless=1")
        elif bitrate_kbps:
            args += ["-b:v", f"{bitrate_kbps}k"]
            if pass_no:
                if name == "libx264":
                    args += ["-pass", str(pass_no), "-passlogfile", passlog]
                else:
                    x265_params += [f"pass={pass_no}", f"stats={passlog}.log"]
        else:
            args += ["-crf", str(quality)]
        if name == "libx265":
            x265_params.append("log-level=error")
            if container == ".mp4":
                args += ["-tag:v", "hvc1"]
    elif name == "libsvtav1":
        args += ["-preset", str(_SVT_PRESET[preset])]
        if bitrate_kbps:
            args += ["-b:v", f"{bitrate_kbps}k"]
        else:
            args += ["-crf", str(quality)]
    elif name.endswith("_nvenc"):
        args += ["-preset", _NVENC_PRESET[preset], "-rc", "vbr"]
        if bitrate_kbps:
            args += ["-b:v", f"{bitrate_kbps}k", "-maxrate", f"{int(bitrate_kbps * 1.5)}k",
                     "-bufsize", f"{bitrate_kbps * 2}k"]
        else:
            args += ["-cq", str(quality), "-b:v", "0"]
        if name == "hevc_nvenc" and container == ".mp4":
            args += ["-tag:v", "hvc1"]
    elif name.endswith("_qsv"):
        args += ["-preset", _QSV_PRESET.get(preset, preset)]
        if bitrate_kbps:
            args += ["-b:v", f"{bitrate_kbps}k", "-maxrate", f"{int(bitrate_kbps * 1.5)}k"]
        else:
            args += ["-global_quality", str(quality)]
    elif name.endswith("_amf"):
        args += ["-quality", _AMF_QUALITY[preset]]
        if bitrate_kbps:
            args += ["-rc", "vbr_peak", "-b:v", f"{bitrate_kbps}k",
                     "-maxrate", f"{int(bitrate_kbps * 1.5)}k"]
        else:
            args += ["-rc", "cqp", "-qp_i", str(quality), "-qp_p", str(quality)]

    if x265_params:
        args += ["-x265-params", ":".join(x265_params)]
    if settings.keyframe_interval and settings.keyframe_interval > 0:
        args += ["-g", str(max(1, int(round(output_fps(settings, info) * settings.keyframe_interval))))]
    # Broadest playback support for 8-bit lossy output.
    if settings.mode != "lossless" and info.pix_fmt in ("yuvj420p", "yuv444p", "yuv422p", "rgb24", "bgra", "bgr0"):
        args += ["-pix_fmt", "yuv420p"]
    return args


def can_copy_audio(codec: str, container: str) -> bool:
    if container == ".mp4" and _MP4_UNSAFE_AUDIO.match(codec or ""):
        return False
    return True


def audio_bitrate_kbps(settings: EncodeSettings, default: int = 192) -> int:
    return int(settings.audio) if settings.audio != "copy" else default


# --------------------------------------------------------------------------
# Target file size
# --------------------------------------------------------------------------

def target_video_bitrate(settings: EncodeSettings, duration: float, audio_kbps_total: float) -> int:
    """Video kbps that lands the whole file near target_size_mb.
    3% is held back for container overhead."""
    if duration <= 0:
        raise FFmpegError("Can't aim for a target size without knowing the duration.")
    total_kbits = settings.target_size_mb * 1024 * 1024 * 8 / 1000 * 0.97
    video = total_kbits / duration - audio_kbps_total
    if video < 50:
        raise FFmpegError(
            f"{settings.target_size_mb:g} MB is too small for {duration:.0f}s of video with this audio "
            f"(it leaves {max(0, video):.0f} kbps for the picture). Trim the clip, lower the audio "
            f"bitrate, drop audio tracks, or raise the target.")
    return int(video)


# --------------------------------------------------------------------------
# Automatic CRF (auto2 heuristic, auto3 measured)
# --------------------------------------------------------------------------

def pick_sample_windows(duration, count: int = 3, length: float = 2.5) -> List[Tuple[float, float]]:
    """Several short windows spread across the clip (around 20%, 50% and
    80%), so one menu or loading screen can't decide the CRF for the whole
    clip. Short clips get one window covering what's there."""
    if not duration or duration <= 0:
        return [(0.0, 4.0)]
    if duration <= count * length * 1.5:
        return [(0.0, min(duration, count * length))]
    return [(max(0.0, duration * f - length / 2), length)
            for f in [(i + 1) / (count + 1) for i in range(count)]]


def build_reference(src: Path, duration: float, tmp: Path, cancel=None,
                    filters: List[str] = ()) -> Tuple[Path, float]:
    """Join the sample windows into one lossless reference clip, with
    `filters` (the encode's own scaling / frame rate cap) applied, so trials
    are measured at the size and rate the real encode makes.
    Returns (path, total seconds)."""
    windows = pick_sample_windows(duration)
    args = []
    for start, length in windows:
        if start > 0:
            args += ["-ss", f"{start:.3f}"]
        args += ["-t", f"{length:.3f}", "-i", str(src)]
    graph = "".join(f"[{i}:v:0]" for i in range(len(windows))) + f"concat=n={len(windows)}:v=1:a=0"
    graph += "".join(f",{f}" for f in filters) + "[v]"
    ref = tmp / "ref.mkv"
    ff.run_checked(args + ["-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-qp", "0",
                           "-preset", "ultrafast", "-an", "-loglevel", "error", "-y", str(ref)], cancel=cancel)
    return ref, sum(l for _, l in windows)


def _encode_kbps(ref: Path, length: float, vargs: List[str], tmp: Path, name: str, cancel) -> float:
    out = tmp / name
    ff.run_checked(["-i", str(ref), "-an"] + vargs + ["-loglevel", "error", "-y", str(out)], cancel=cancel)
    size = out.stat().st_size if out.exists() else 0
    ff.safe_unlink(out)
    return size * 8 / max(length, 0.1) / 1000


def _codec_offset(settings: EncodeSettings) -> int:
    # H.265 needs a numerically higher CRF than H.264 for the same look;
    # SVT-AV1's scale runs to 63, roughly +10 from x264's.
    return {"h264": 0, "h265": 5, "av1": 10}[settings.codec]


def estimate_crf_heuristic(src: Path, info: MediaInfo, settings: EncodeSettings,
                           cancel: CancelToken = None) -> int:
    """auto2: quick x264 sample encodes of several spots estimate complexity
    (CRF 30 bitrate, log scale) and how noise-like the detail is (CRF14 /
    CRF30 ratio). Calibrated on synthetic clips only (handoff 7.4)."""
    with tempfile.TemporaryDirectory(prefix="trimmer_auto2_") as td:
        tmp = Path(td)
        ref, length = build_reference(src, info.duration, tmp, cancel, video_filters(settings, info))
        fast = ["-c:v", "libx264", "-preset", "ultrafast"]
        hi = _encode_kbps(ref, length, fast + ["-crf", "30"], tmp, "hi.mp4", cancel)
        lo = _encode_kbps(ref, length, fast + ["-crf", "14"], tmp, "lo.mp4", cancel)
    ratio = (lo / hi) if hi > 0 else 1.0
    a_lo_k, a_lo_c, a_hi_k, a_hi_c = 10.0, 32.0, 30000.0, 17.0
    k = max(a_lo_k, min(a_hi_k, hi))
    frac = (math.log10(k) - math.log10(a_lo_k)) / (math.log10(a_hi_k) - math.log10(a_lo_k))
    baseline = a_lo_c - (a_lo_c - a_hi_c) * frac
    adjustment = max(-6.0, min(4.0, (ratio - 3.0) * 2.2))
    off = _codec_offset(settings)
    crf = baseline + adjustment + off
    return int(round(max(15 + off, min(34 + off, crf))))


# Bitrates of auto3's own trial encodes, so predicting the size of the CRF it
# picked costs nothing: (source, its size and date, settings, CRF) -> kbps.
_trial_kbps: Dict[tuple, float] = {}


def _trial_key(src: Path, settings: EncodeSettings, quality: int) -> tuple:
    try:
        st = Path(src).stat()
        stamp = (st.st_size, st.st_mtime)
    except OSError:
        stamp = None
    return (str(src), stamp, json.dumps(settings.to_dict(), sort_keys=True), quality)


def predict_size(src: Path, info: MediaInfo, settings: EncodeSettings, quality: int,
                 seconds: float, cancel: CancelToken = None) -> int:
    """Estimated output bytes for `seconds` of this clip at these settings,
    from encoding the sample windows with the real encoder and preset (or
    auto3's trial at that CRF, when it already made one). Audio is estimated
    from the source's track bitrates (or the chosen AAC bitrate)."""
    kbps = _trial_kbps.get(_trial_key(src, settings, quality))
    if kbps is None:
        with tempfile.TemporaryDirectory(prefix="trimmer_predict_") as td:
            tmp = Path(td)
            ref, length = build_reference(src, info.duration, tmp, cancel)
            vargs = video_codec_args(settings, info, quality=quality, container=".mkv")
            vf = video_filters(settings, info)
            if vf:
                vargs += ["-vf", ",".join(vf)]
            kbps = _encode_kbps(ref, length, vargs, tmp, "predict.mkv", cancel)
    if settings.audio == "copy":
        akbps = sum((a.bit_rate or 160000) for a in info.audio) / 1000
    else:
        akbps = int(settings.audio) * len(info.audio)
    return int((kbps + akbps) * 1000 / 8 * seconds * 1.01)


def _quality_score(ref: Path, trial: Path, use_vmaf: bool, tmp: Path, cancel) -> float:
    if use_vmaf:
        # Run inside the temp folder and give libvmaf a bare relative file
        # name. A full Windows path (C:\...) inside a filter string breaks on
        # the ':' and '\' and was the auto3 crash (handoff 7.1).
        ff.run_checked(["-i", str(trial), "-i", str(ref),
                        "-lavfi", "[0:v][1:v]libvmaf=log_fmt=json:log_path=vmaf.json",
                        "-f", "null", "-", "-loglevel", "error"], cwd=str(tmp), cancel=cancel)
        data = json.loads((tmp / "vmaf.json").read_text(encoding="utf-8"))
        return float(data["pooled_metrics"]["vmaf"]["mean"])
    out = ff.run_checked(["-i", str(trial), "-i", str(ref), "-lavfi", "[0:v][1:v]ssim",
                          "-f", "null", "-"], cancel=cancel)
    m = re.search(r"All:([\d.]+)", out.stdout + out.stderr)
    return float(m.group(1)) * 100 if m else 0.0


def ssim_target(vmaf_target: float) -> float:
    """The SSIM (x100) used instead of a VMAF target when libvmaf is missing:
    VMAF 93 -> 97, each VMAF point half an SSIM point."""
    return min(99.5, max(90.0, 97.0 + (vmaf_target - 93.0) / 2))


def estimate_crf_measured(src: Path, info: MediaInfo, settings: EncodeSettings,
                          cancel: CancelToken = None, log=None) -> Tuple[int, str]:
    """auto3: encode a short sample at candidate CRFs with the real encoder
    and preset, score each against a lossless reference (VMAF if available,
    else SSIM), and keep the highest CRF that meets the target.

    Returns (crf, metric_name)."""
    log = log or (lambda msg: None)
    use_vmaf = ff.libvmaf_available()
    off = _codec_offset(settings)
    lo_bound, hi_bound = 15 + off, 34 + off
    with tempfile.TemporaryDirectory(prefix="trimmer_auto3_") as td:
        tmp = Path(td)
        ref, length = build_reference(src, info.duration, tmp, cancel, video_filters(settings, info))

        lo, hi = lo_bound, hi_bound
        best = None
        tried = []
        for _ in range(4):
            if lo > hi:
                break
            mid = (lo + hi) // 2
            trial = tmp / f"trial_{mid}.mkv"
            vargs = video_codec_args(settings, info, quality=mid, container=".mkv")
            ff.run_checked(["-i", str(ref), "-an"] + vargs + ["-loglevel", "error", "-y", str(trial)],
                           cancel=cancel)
            target = settings.auto3_target if use_vmaf else ssim_target(settings.auto3_target)
            try:
                score = _quality_score(ref, trial, use_vmaf, tmp, cancel)
            except (FFmpegError, OSError, KeyError, ValueError) as e:
                if not use_vmaf:
                    raise
                # VMAF failed: say why once, then carry on with SSIM rather
                # than crashing the whole batch.
                log(f"VMAF measurement failed, falling back to SSIM: {str(e)[-300:]}")
                use_vmaf = False
                target = ssim_target(settings.auto3_target)
                score = _quality_score(ref, trial, False, tmp, cancel)
            if trial.exists():
                _trial_kbps[_trial_key(src, settings, mid)] = trial.stat().st_size * 8 / max(length, 0.1) / 1000
            tried.append(mid)
            if score >= target:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
            ff.safe_unlink(trial)
        if best is None:
            # Nothing met the target: use the most conservative CRF actually
            # tried instead of the midpoint the old code returned (7.2).
            best = min(tried) if tried else lo_bound
            log(f"No tested CRF reached the quality target; using the safest one tried (CRF {best}).")
    return best, ("VMAF" if use_vmaf else "SSIM")


def resolve_quality(src: Path, info: MediaInfo, settings: EncodeSettings,
                    cancel: CancelToken = None, log=None) -> Tuple[Optional[int], Optional[str]]:
    """Return (quality, method_tag). Fixed CRF -> (n, None); auto -> analysed."""
    if settings.mode != "lossy":
        return None, None
    if settings.quality == "auto2":
        return estimate_crf_heuristic(src, info, settings, cancel), "auto2"
    if settings.quality == "auto3":
        crf, _metric = estimate_crf_measured(src, info, settings, cancel, log)
        # A target other than the default goes in the name: "CRF21 auto3-95".
        return crf, "auto3" if settings.auto3_target == 93.0 else f"auto3-{settings.auto3_target:g}"
    return int(settings.quality), None
