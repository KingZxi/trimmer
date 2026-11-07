"""Replay merge jobs: the replays are grouped into sessions, and each session is merged into one
video (or trimmed into one file per replay) by exact packet matching when the replays share
their encoded frames (merge_packets), else by the audio and timestamp estimate (merge_fallback)."""

import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import List, Optional

from .. import ffmpeg as ff
from .. import merge as mg
from .. import packets as pk
from ..encode import EncodeSettings
from ..ffmpeg import FFmpegError
from ..render import AudioPlan, Span, render
from ..tag import build_tag
from ..util import sanitize_filename
from .clip_job import _write_rip, run_clip_job
from .merge_fallback import _fallback_session
from .merge_packets import _packet_session
from .model import VIDEO_ONLY, ClipJob, JobContext, JobOptions, JobResult, MergeJob, _Skip
from .output import _check_space, _claim, _delete_sources, _keep_note, _out_dir, _save, _write_text


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

                def write(part):
                    return render([Span(path, info, 0.0, None)], part, encode=None, quality=None,
                                  audio=AudioPlan(tracks=[]), on_progress=ctx.reporter("Ripping video"),
                                  cancel=ctx.cancel, log=ctx.log, tag=build_tag(info.trimmer_tag, ["video only"]))
                return _save(ctx, final, write, expected=info.duration, n_audio=0, date_source=date_src,
                             what="Video-only file", failed="Ripping the video failed")
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
