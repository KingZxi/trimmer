"""Saving outputs: names (and the name-clash policy), the hidden tags, writing to ".part" then
checking and renaming (_save), "keep the original if compressing would make it bigger"
(_keep_smaller), and deleting originals once every output is saved."""

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .. import ffmpeg as ff
from ..encode import EncodeSettings, predict_size, quality_label
from ..ffmpeg import Cancelled, FFmpegError
from ..render import AudioPlan, verify_output
from ..tag import build_tag, describe_encode
from ..util import (format_hms, format_name_time, format_size, recycle_or_delete, safe_unlink, same_file,
                    trim_range_label, unique_path)
from .checks import Window, _note_source_gaps, _verify_frames
from .model import JobContext, JobOptions, JobResult, MergeJob, _Skip


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


def _trim_tag(opts: JobOptions, ranges) -> str:
    """'(trimmed 1m05s-1m32s)' for the kept (start, stop) ranges, or "" when
    nothing was cut or the times are turned off in Settings."""
    if not (opts.add_operation_tags and opts.add_trim_range and ranges):
        return ""
    return f" ({trim_range_label(ranges)})"


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


class _TooLarge(Exception):
    """A compressed save grew past the size it had to stay under (and was deleted)."""

    def __init__(self, size: int):
        super().__init__(size)
        self.size = size


def _save(ctx, final: Path, write: Callable[[Path], Optional[ff.RunResult]], *, expected: float, n_audio,
          date_source: Path, expect_video: bool = True, check: Callable[[Path], Optional[str]] = None,
          frames: Tuple[float, List[Window]] = None, gaps: Tuple[float, List[Window]] = None,
          stage: str = "Verifying", what: str = "Output", failed: str = "Saving failed",
          rejected: str = None) -> Path:
    """Every output is saved the same way: written to "<name>.part<ext>", checked, and only then
    given its real name (`final`, already claimed), so nothing half-written ever looks finished.

    write(part) writes the file and returns ffmpeg's result (None when it copied the file itself).
    check(part) is an extra check after the usual one, returning a problem or None.
    frames=(fps, source windows): decode the output and check every frame is there (encodes).
    gaps=(fps, source windows): only report the gaps a copy carries over from the source.
    Raises _TooLarge when ffmpeg stopped at its size limit. Whatever goes wrong, the .part goes."""
    part = _part(final)
    try:
        r = write(part)
        if r is not None and r.status == "too_large":
            raise _TooLarge(r.last_size)
        if r is not None and not r.ok:
            raise ff.failure(failed, r)
        ctx.stage(stage)
        problem = verify_output(part, expected, expect_video, n_audio)
        if not problem and check is not None:
            problem = check(part)
        if problem:
            _reject(ctx, part, final, problem, rejected or f"{what} failed verification")
        if frames is not None:
            _verify_frames(ctx, part, expected, frames[0], frames[1], what)
        if gaps is not None:
            _note_source_gaps(ctx, gaps[1], gaps[0])
        _finish(part, final, date_source)
    except BaseException:
        try:
            safe_unlink(part)
        except OSError:
            pass                # still held open: the error that got here matters more
        raise
    return final


@dataclass
class _SizeRule:
    """"Keep the original if compressing would make it bigger": the size a compressed save must
    stay under (None: no limit here), and how the log names the uncompressed alternative."""
    plain: Optional[int]
    versus: str = "the original"
    instead: str = "keeping the original footage"


def _merge_rule(enc: Optional[EncodeSettings], audio: AudioPlan, plain: Callable[[], Optional[int]]) -> _SizeRule:
    """The rule for a merge, compared with a plain (stream-copied) merge. plain() is its size, or
    None when no plain merge is possible. A mix re-encodes the audio anyway, and a target size
    already decides the size, so neither has a limit."""
    applies = enc is not None and enc.revert_if_larger and enc.mode != "target_size" and not audio.needs_filter
    return _SizeRule(plain() if applies else None, "a plain merge", "merging without compressing")


def _keep_smaller(ctx, rule: _SizeRule, compressed: Callable[[Optional[int]], object],
                  plain: Callable[[], object], predict: tuple = None):
    """Save compressed, or uncompressed when that is smaller.

    compressed(limit) saves the compressed output; its _save raises _TooLarge once it passes the
    limit. plain() saves the uncompressed output instead. predict=(encode settings, quality, source,
    info, seconds) first asks a short sample encode, so a clear case never starts the real encode."""
    if rule.plain is None:
        return compressed(None)
    if predict is not None:
        enc, q, src, info, seconds = predict
        if enc.mode == "lossy" and q is not None:
            ctx.stage("Predicting output size")
            guess = predict_size(src, info, enc, q, seconds, ctx.cancel)
            if guess > rule.plain * 1.05:
                ctx.log(f"Predicted size {format_size(guess)} is larger than {rule.versus} "
                        f"({format_size(rule.plain)}); skipping the encode and {rule.instead}.")
                return plain()
    try:
        return compressed(rule.plain)
    except _TooLarge as e:
        ctx.log(f"Compressed output would be larger than {rule.versus} ({format_size(e.size)} vs "
                f"{format_size(rule.plain)}); {rule.instead} instead.")
        return plain()


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


def _write_text(path: Path, text: str):
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass
