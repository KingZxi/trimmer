"""The hidden "Trimmer" tag written into every file the app saves.

It lives in the container's `comment` tag (MP4/MOV keep it as (c)cmt; MKV/MKA,
FLAC, MP3 and WAV have their own comment field), e.g.

    Trimmer: trimmed; then compressed (H.265, q28)

The tag is cumulative: a file made from an already tagged one lists the old
operations first. Outputs are written with -map_metadata -1, so the previous
tag is carried over here, by hand. The same text is what the queue warnings
read back to spot work that is about to be done twice.
"""

import re
from pathlib import Path
from typing import List

PREFIX = "Trimmer:"
_THEN = "; then "

_CODEC_NAMES = {"h264": "H.264", "h265": "H.265", "av1": "AV1"}

# What the name tags and the Merged default name look like (see jobs._tags,
# encode.quality_label and jobs.session_names).
_NAME_TRIM = re.compile(r"\([^()]*\btrimmed\b[^()]*\)", re.I)
_NAME_COMPRESS = re.compile(r"\((?:LOSSY|LOSSLESS)\)|\(TARGET [\d.]+MB\)|\((?:CRF|CQ)\d+", re.I)
_NAME_MERGED = re.compile(r"^Merged \d{4}-\d\d-\d\d \d\d-\d\d-\d\d to \d\d-\d\d-\d\d")

_VERB = {"trim": "trimmed", "compress": "compressed", "merge": "merged"}


def describe_encode(enc, quality=None) -> str:
    """'H.265, q28' / 'H.264, 9.5 MB' / 'H.264, lossless' for the tag."""
    codec = _CODEC_NAMES.get(enc.codec, enc.codec)
    if getattr(enc, "uses_ten_bit", False):
        codec += " 10-bit"
    if enc.mode == "lossless":
        return f"{codec}, lossless"
    if enc.mode == "target_size":
        return f"{codec}, {enc.target_size_mb:g} MB"
    if quality is not None:
        return f"{codec}, q{quality}"
    return codec


def build_tag(previous: str, ops: List[str]) -> str:
    """The tag for a new output: `previous` (the source's tag, if any) then
    this job's operations. No operations -> the previous tag is kept as is."""
    ops = [o for o in ops if o]
    previous = previous or ""
    if not ops:
        return previous
    body = ", ".join(ops)
    if previous.startswith(PREFIX):
        return f"{previous}{_THEN}{body}"
    return f"{PREFIX} {body}"


def tag_args(tag: str) -> List[str]:
    """ffmpeg output options that write the tag (empty when there is none)."""
    return ["-metadata", f"comment={tag}"] if tag else []


def tag_kinds(tag: str) -> set:
    """Which of trim / compress / merge a tag says were done."""
    return {kind for word, kind in (("trimmed", "trim"), ("compressed", "compress"), ("merged", "merge"))
            if re.search(rf"\b{word}\b", tag)}


def name_kinds(stem: str, name_suffix: str = "") -> set:
    """The same, guessed from Trimmer's own name tags (for files with no tag).
    "any" means the configured name suffix is in the name."""
    kinds = set()
    if _NAME_TRIM.search(stem):
        kinds.add("trim")
    if _NAME_COMPRESS.search(stem):
        kinds.add("compress")
    if _NAME_MERGED.search(stem):
        kinds.add("merge")
    if name_suffix.strip() and name_suffix in stem:
        kinds.add("any")
    return kinds


def job_kinds(job) -> set:
    """The kinds of work a queued job would do."""
    kinds = set()
    if job.kind == "merge":
        kinds.add("trim" if job.options.merge_mode == "trim" else "merge")
    elif job.segments:
        kinds.add("trim")
    if job.compress:
        kinds.add("compress")
    return kinds


def already_done(job, path, info=None) -> str:
    """"" when `path` hasn't had the job's kind of work, else a short
    description of what it already went through (for the warning dialog)."""
    wanted = job_kinds(job)
    if not wanted:
        return ""
    tag = getattr(info, "trimmer_tag", "") if info is not None else ""
    if tag:
        hit = wanted & tag_kinds(tag)
        return f"already {' and '.join(_VERB[k] for k in sorted(hit))} ({tag})" if hit else ""
    kinds = name_kinds(Path(path).stem, job.options.name_suffix)
    hit = wanted & kinds
    if hit:
        return f"its name says it is already {' and '.join(_VERB[k] for k in sorted(hit))}"
    if "any" in kinds:
        return "its name carries your Trimmer name suffix"
    return ""
