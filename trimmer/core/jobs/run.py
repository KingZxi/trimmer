"""Running a job: auto settings first (analysed now if the queue hasn't already), then the job;
failures become a "failed" result, so only Cancelled ever leaves run_job."""

from dataclasses import replace
from pathlib import Path

from .. import ffmpeg as ff
from ..encode import EncodeSettings, pick_settings
from ..ffmpeg import Cancelled, CancelToken, FFmpegError, Progress
from .clip_job import run_clip_job
from .merge_job import run_merge_job
from .model import JobContext, JobResult, _Skip


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
