"""A session whose replays don't share encoded frames: overlaps worked out from the timestamps
and the audio, with a safety margin, then cut on keyframes or re-encoded for exact cuts."""

from pathlib import Path
from typing import List, Optional

from .. import merge as mg
from ..encode import EncodeSettings, resolve_quality
from ..ffmpeg import FFmpegError
from ..render import AudioPlan, Span, copy_compatible, output_ext, render
from ..util import format_hms_ms, sanitize_filename
from .checks import _gap_section, _span_windows
from .model import EXACT_SETTINGS, NOT_COMPRESSED
from .output import _claim, _keep_smaller, _merge_rule, _merge_tag, _qlabel, _save, _trim_tag, _write_text


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
            final, span = _fallback_clip(job, ctx, e.clip, e.trim_start, enc, audio, folder, ext, srcs,
                                         f"Clip {i + 1} of {len(plan)}")
            outputs.append(final)
            written.append((e.clip, final.name, span.start))
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

    def save(sp: List[Span], e_, q_, extra: str = "", limit: Optional[int] = None):
        """The merge of spans `sp`, encoded with e_ (None: stream copy). Returns (path, spans)."""
        label = _qlabel(opts, e_, q_, method) if e_ is not None and enc is not None else ""
        final = _claim(out_root / f"{sanitize_filename(merged_name + opts.name_suffix)}{extra}{label}{ext}",
                       ctx, opts, srcs)
        tag = _merge_tag(job, first.info.trimmer_tag, audio, len(first.info.audio), enc if e_ is not None else None,
                         q_)
        windows = (first.info.fps, _span_windows(sp))

        def write(part):
            return render(sp, part, encode=e_, quality=q_, audio=audio, on_progress=ctx.reporter("Merging"),
                          cancel=ctx.cancel, log=ctx.log, normalize_audio=e_ is not None, tag=tag, size_limit=limit)
        return _save(ctx, final, write, expected=sum(s.length for s in sp), n_audio=None,
                     date_source=kept[-1].path, frames=windows if e_ is not None else None,
                     gaps=None if e_ is not None else windows, what="Merge"), sp

    if use_enc is None:
        final, used = save(copied, None, None)
    else:
        def plain_size():       # only worth comparing when a plain merge is possible
            nonlocal copied
            copied, _reason = copy_plan()
            return None if copied is None else int(sum(
                s.info.size * s.length / s.info.duration if s.info.duration else s.info.size for s in copied))
        final, used = _keep_smaller(ctx, _merge_rule(enc, audio, plain_size),
                                    lambda limit: save(spans, use_enc, q, limit=limit),
                                    lambda: save(copied, None, None, NOT_COMPRESSED),
                                    predict=(enc, q, first.path, first.info, sum(s.length for s in spans)))
    ctx.log(f"Saved {final.name}")
    outputs.append(final)
    cuts = [(e.clip, s.start) for e, s in zip(plan, used)]
    pos = 0.0
    for (c, t), nxt in zip(cuts, [c for c, _ in cuts[1:]]):
        pos += c.duration - t
        ctx.log(f"Join at {format_hms_ms(pos)}: {c.path.name} -> {nxt.path.name}")
    if opts.generate_clip_log:
        _write_text(final.with_name(f"{final.stem} - info.txt"), mg.clip_log_for_merge(session, cuts, mopts)
                    + _gap_section(ctx.warnings[mark:]))
    return outputs


def _fallback_clip(job, ctx, c: mg.ReplayClip, cut: float, enc, audio: AudioPlan, folder: Path, ext: str, srcs,
                   stage: str):
    """One clip of a trim-mode session the fallback method planned, saved from `cut` on (or the
    keyframe before it, for a stream copy). Returns (saved path, the span really saved)."""
    opts = job.options
    mopts = opts.merge_options()
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

    def save(sp: Span, e_, q_, extra: str = "", limit: Optional[int] = None):
        trim = _trim_tag(opts, [(sp.start, c.info.duration)] if sp.start > 0.01 else [])
        label = _qlabel(opts, e_, q_, method) if enc is not None and e_ is not None else ""
        final = _claim(folder / f"{sanitize_filename(c.path.stem + opts.name_suffix)}{trim}{extra}{label}{ext}",
                       ctx, opts, srcs)
        ctx.stage(stage)
        tag = _merge_tag(job, c.info.trimmer_tag, audio, len(c.info.audio), enc if e_ is not None else None, q_)

        def write(part):
            return render([sp], part, encode=e_, quality=q_, audio=audio, on_progress=ctx.reporter(stage),
                          cancel=ctx.cancel, log=ctx.log, tag=tag, size_limit=limit)
        return _save(ctx, final, write, expected=sp.length, n_audio=None, date_source=c.path,
                     gaps=(c.info.fps, _span_windows([sp])), what=c.path.name), sp

    copy_span = None

    def plain_size():           # the plain copy starts on the keyframe at or before the cut
        nonlocal copy_span
        if opts.merge_method == "exact":
            return None
        kf = span.start if span.start <= 0.01 else \
            mg.pick_lossless_cut(c.path, span.start, mopts.never_lose, mopts.lossless_max_repeat)[0]
        if kf is None:
            return None
        copy_span = Span(c.path, c.info, kf, None)
        return int(c.info.size * copy_span.length / c.info.duration) if c.info.duration else c.info.size

    return _keep_smaller(ctx, _merge_rule(enc, audio, plain_size), lambda limit: save(span, use_enc, q, limit=limit),
                         lambda: save(copy_span, None, None, NOT_COMPRESSED),
                         predict=(enc, q, c.path, c.info, span.length))
