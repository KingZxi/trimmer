"""How big an output will be, before it is made. The Clips tab's "Estimate size" and the sizes in
the Queue both come from here, so they agree with each other and with what the job does."""

from dataclasses import replace
from pathlib import Path
from typing import Optional, Tuple

from . import ffmpeg as ff
from . import jobs as J
from .encode import EncodeSettings, pick_settings, predict_size, resolve_quality
from .ffmpeg import CancelToken, MediaInfo


def estimate_size(src: Path, info: MediaInfo, kept: float, enc: Optional[EncodeSettings], *,
                  quick: bool = False, analyse: bool = False, cancel: CancelToken = None) -> Optional[int]:
    """About how big a video of `kept` seconds of `src` comes out; enc None is a plain copy.

    None when that can't be told in advance: a lossless encode (it depends too much on the
    footage), or auto settings not analysed yet (analyse=True analyses them now, which takes
    minutes). quick: Auto 3 is estimated with Auto 2's analysis, so this never runs VMAF.
    With "keep the original if compressing would make it bigger" on, it is never more than the
    plain copy, because that is what the job keeps then."""
    plain = int(info.size * kept / info.duration) if info.duration else info.size
    if enc is None:
        return plain
    if analyse and enc.needs_analysis:
        enc = replace(enc, chosen=pick_settings(src, info, enc, cancel))
    enc = enc.applied()
    if enc.is_autoset:
        return None
    if enc.mode == "target_size":
        est = int(enc.target_size_mb * 1024 * 1024)
    elif enc.mode == "lossless":
        est = None
    else:
        probe = replace(enc, quality="auto2") if quick and enc.quality == "auto3" else enc
        q, _ = resolve_quality(src, info, probe, cancel)
        est = predict_size(src, info, enc, q, kept, cancel)
    if enc.revert_if_larger:
        est = plain if est is None else min(est, plain)
    return est


def kept_seconds(job: J.ClipJob, duration: float) -> float:
    """How much of its source a clip job keeps."""
    if not job.segments:
        return duration
    return sum((duration if e is None else min(e, duration)) - s for s, e in job.segments)


def estimate_job(job) -> Tuple[Optional[int], Optional[int]]:
    """(about how big the job's video comes out, or None; how big its originals are). Quick: it
    never runs VMAF (see estimate_size)."""
    if isinstance(job, J.MergeJob):
        total = 0
        for p in job.clip_paths:
            try:
                total += ff.probe(p).size
            except Exception:
                pass
        return None, total or None
    src = Path(job.source)
    info = ff.probe(src)
    if not job.make_video:
        return None, info.size
    enc = EncodeSettings.from_dict(job.encode) if job.compress else None
    return estimate_size(src, info, kept_seconds(job, info.duration or 0), enc, quick=True), info.size


def prepare_job(job, cancel: CancelToken = None, on_step=None):
    """(about how big the job's video comes out or None, how big its originals are, the chosen auto
    settings or None). Auto settings are analysed first (minutes, not seconds), then the size is
    estimated for what they chose."""
    chosen = None
    if J.needs_analysis(job):
        chosen = J.analyse_job(job, cancel, on_step=on_step)
        job = replace(job, encode={**job.encode, "chosen": chosen})
    est, in_bytes = estimate_job(job)
    return est, in_bytes, chosen
