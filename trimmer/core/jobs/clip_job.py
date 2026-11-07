"""Clip jobs: one source file, optionally trimmed, compressed, with chosen or mixed audio
tracks, and optional audio and video-only rips."""

import shutil
from pathlib import Path
from typing import List, Optional

from .. import ffmpeg as ff
from ..encode import EncodeSettings, can_copy_audio, resolve_quality
from ..ffmpeg import FFmpegError
from ..render import (AudioPlan, Span, output_ext, render, rip_audio, rip_extension, snap_spans_to_keyframes,
                      verify_output)
from ..smartcut import SmartCutError, smart_render
from ..tag import build_tag, describe_encode
from ..util import VIDEO_EXTENSIONS, format_hms_ms, safe_unlink, sanitize_filename
from .checks import _note_source_gaps, _span_windows, check_copy_trim
from .model import EXACT_SETTINGS, NOT_COMPRESSED, VIDEO_ONLY, ClipJob, JobContext, JobOptions, JobResult, _Skip
from .output import (_check_space, _claim, _delete_sources, _finish, _keep_note, _keep_smaller, _out_dir, _part,
                     _qlabel, _save, _SizeRule, _trim_tag)


def _base_stem(job: ClipJob, src: Path) -> str:
    """The user's chosen name, else the source name plus the configured suffix."""
    name = job.output_name.strip()
    if Path(name).suffix.lower() in VIDEO_EXTENSIONS | {".wav", ".flac", ".mp3", ".m4a"}:
        name = Path(name).stem          # "Highlight.mp4" -> "Highlight"; the extension is ours to pick
    name = sanitize_filename(name) if name else ""
    return name or sanitize_filename(src.stem + job.options.name_suffix)


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
    ext = output_ext(src, opts.container)
    stem = _base_stem(job, src)
    video_only = name_extra == VIDEO_ONLY
    kept = sum(s.length for s in spans)
    enc = EncodeSettings.from_dict(job.encode) if job.compress else None

    def claim(extra: str = "") -> Path:
        return _claim(out_dir / f"{stem}{_tags(job, spans)}{name_extra}{extra}{ext}", ctx, opts, [src])

    def copy(extra: str = "") -> Path:
        _snap(ctx, spans)       # before naming, so the kept times in the name are the real ones
        return _write_copy(job, ctx, info, spans, claim(extra), audio,
                           _clip_tag(job, info, audio, video_only=video_only))

    exact = None            # the name a frame-exact trim already claimed
    if enc is None:
        if not (job.segments and opts.exact_cuts):
            done = copy()
            _note_rip_source(res, done, info, audio, name_extra)
            return done
        if opts.smart_cut:
            exact = claim()
            done = _write_smart(job, ctx, info, spans, exact, audio, _clip_tag(job, info, audio, video_only=video_only))
            if done is not None:
                _note_rip_source(res, done, info, audio, name_extra)
                return done
        enc = EncodeSettings.from_dict(EXACT_SETTINGS.to_dict())

    problems = enc.problems()
    if problems:
        raise FFmpegError(" ".join(problems))
    if "q" not in label_cache:
        if enc.is_auto:
            ctx.stage(f"Analysing quality ({enc.quality})")
        label_cache["q"] = resolve_quality(src, info, enc, ctx.cancel, ctx.log)
    q, method = label_cache["q"]
    if q is not None and job.compress:
        ctx.log(f"Using {'CQ' if enc.encoder != 'software' else 'CRF'} {q}"
                + (f" (chosen by {method})" if method else ""))
    label = _qlabel(opts, enc, q, method) if job.compress else ""
    tag = _clip_tag(job, info, audio, enc if job.compress else None, q, video_only)

    def compressed(limit: Optional[int]) -> Path:
        def write(part):
            return render(spans, part, encode=enc, quality=q, audio=audio, on_progress=ctx.reporter("Encoding"),
                          cancel=ctx.cancel, size_limit=limit, log=ctx.log, tag=tag)
        return _save(ctx, exact or claim(label), write, expected=kept, n_audio=_n_out_audio(audio, len(info.audio)),
                     date_source=src, frames=(info.fps, _span_windows(spans)))

    def plain() -> Path:
        res.status = "reverted"
        return copy(NOT_COMPRESSED)

    original_equiv = int(info.size * (kept / info.duration)) if info.duration else info.size
    rule = _SizeRule(original_equiv if job.compress and enc.revert_if_larger else None)
    return _keep_smaller(ctx, rule, compressed, plain, predict=(enc, q, src, info, kept))


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
        ctx.log(f"Smart cut failed ({ff.explain_failure(r.stderr_tail)}); re-encoding the trim instead.\n"
                f"{r.stderr_tail[-400:]}")
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
                tag: str = "") -> Path:
    """Stream copy of spans already snapped to keyframes (see _snap)."""
    src = Path(job.source)
    whole = len(spans) == 1 and spans[0].start <= 0.0005 and spans[0].end is None

    def write(part):
        if whole and audio.tracks is None and not audio.needs_filter and not tag \
                and final.suffix.lower() == src.suffix.lower():
            ctx.stage("Copying")
            shutil.copyfile(src, part)
            ctx.cancel.check()
            return None
        return render(spans, part, encode=None, quality=None, audio=audio,
                      on_progress=ctx.reporter("Copying (no re-encode)"), cancel=ctx.cancel, tag=tag)

    def starts_right(part):
        """A single cut part: the copy must start and end where it should."""
        return check_copy_trim(src, part, spans[0].start, spans[0].end)

    return _save(ctx, final, write, expected=sum(s.length for s in spans),
                 n_audio=_n_out_audio(audio, len(info.audio)), date_source=src,
                 check=starts_right if len(spans) == 1 and not whole else None,
                 gaps=(info.fps, _span_windows(spans)))


def _write_rip(ctx: JobContext, opts: JobOptions, spans: List[Span], t: int, name: str, out_dir: Path,
               sources: List[Path], date_source: Path) -> Path:
    """One audio track of the spans as its own file, verified before it gets its name."""
    fmt = opts.rip_format
    if fmt == "copy" and len(spans) > 1:
        fmt = "flac"     # joining several parts needs a re-encode; keep it lossless
    final = _claim(out_dir / f"{name}{rip_extension(spans[0].info.audio[t], fmt)}", ctx, opts, sources)
    ctx.stage(f"Ripping audio track {t + 1}")
    ops = (["trimmed"] if any(s.start > 0.0005 or (s.end is not None and s.end < s.info.duration - 0.0005)
                              for s in spans) else []) + ["audio rip"]
    tag = build_tag(spans[0].info.trimmer_tag, ops)

    def write(part):
        r, _path = rip_audio(spans, t, fmt, part.with_suffix(""), ctx.reporter(f"Ripping audio track {t + 1}"),
                             ctx.cancel, tag=tag)
        return r

    return _save(ctx, final, write, expected=sum(s.length for s in spans), n_audio=1, expect_video=False,
                 date_source=date_source, what=f"Track {t + 1}", failed=f"Ripping audio track {t + 1} failed")


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
