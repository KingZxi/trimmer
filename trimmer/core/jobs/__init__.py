"""Jobs: what the queue runs.

A ClipJob is one source file: optional trim segments, an optional
compress, a choice of audio tracks (and an optional mix), plus optional
track rips. A MergeJob is one OBS replay session: merge into one video or
trim overlaps into separate files, optionally compressed in the same pass.

Every output is written to "<name>.part<ext>", verified, and only then
renamed to its real name, so an interrupted run never leaves a broken file
that looks finished (output._save). Originals are only deleted (Recycle Bin
by default) after every output of the job has been verified.

  model           the jobs, their options and result, the callbacks to the runner
  run             run_job: auto settings, then the job; failures become a result
  clip_job        clip jobs
  merge_job       replay merge jobs: sessions, rips
  merge_packets   sessions whose replays share encoded frames (exact)
  merge_fallback  the others (timestamps, audio alignment, safety margin)
  output          names, tags, saving and checking each output, deleting originals
  checks          the "nothing lost" checks and gaps already in the source
"""

from .checks import SourceGap, check_copy_trim, check_frames, decoded_gaps, frame_gaps, source_gaps
from .clip_job import run_clip_job
from .merge_job import run_merge_job, session_names
from .model import (EXACT_SETTINGS, NOT_COMPRESSED, VIDEO_ONLY, ClipJob, JobContext, JobOptions, JobResult, MergeJob,
                    job_from_dict, job_to_dict)
from .run import analyse_job, analysis_source, needs_analysis, run_job

__all__ = [
    "EXACT_SETTINGS", "NOT_COMPRESSED", "VIDEO_ONLY", "ClipJob", "JobContext", "JobOptions", "JobResult", "MergeJob",
    "SourceGap", "analyse_job", "analysis_source", "check_copy_trim", "check_frames", "decoded_gaps", "frame_gaps",
    "job_from_dict", "job_to_dict", "needs_analysis", "run_clip_job", "run_job", "run_merge_job", "session_names",
    "source_gaps",
]
