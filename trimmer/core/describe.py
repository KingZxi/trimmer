"""Jobs in words: the Queue's one-line summary of a job, the first line of its details, and
the note about gaps a finished job found in its source. Kept out of the GUI so it can be tested
without Qt, and so the codec and quality are worded one way everywhere."""

import re
from pathlib import Path

from . import jobs as J
from .encode import CODECS, EncodeSettings

_CODEC_NAMES = {"h264": "H.264", "h265": "H.265", "av1": "AV1"}


def codec_name(e: EncodeSettings) -> str:
    """"H.265 10-bit", "AV1", ..."""
    return _CODEC_NAMES.get(e.codec, CODECS.get(e.codec, e.codec)) + (" 10-bit" if e.uses_ten_bit else "")


def quality_text(e: EncodeSettings, short: bool = False) -> str:
    """The quality a compression aims for: "CRF 23" (short: "CRF23"), "CQ 30" on a hardware encoder,
    "Auto 3 (target 95)" (short: "auto3 (target 95)", the target only when not the usual 93),
    "target size 50 MB" (short: "50 MB"), or "lossless"."""
    if e.mode == "lossless":
        return "lossless"
    if e.mode == "target_size":
        return f"{e.target_size_mb:g} MB" if short else f"target size {e.target_size_mb:g} MB"
    if e.quality == "auto2":
        return "auto2" if short else "Auto 2"
    if e.quality == "auto3":
        if short:
            return "auto3" + (f" (target {e.auto3_target:g})" if e.auto3_target != 93.0 else "")
        return f"Auto 3 (target {e.auto3_target:g})"
    word = "CQ" if e.encoder != "software" else "CRF"
    return f"{word}{e.quality}" if short else f"{word} {e.quality}"


def gap_note(result, short=False) -> str:
    """The source problems a finished job reported (frame gaps it kept), in one line."""
    w = getattr(result, "warnings", None) or []
    if not w:
        return ""
    first = w[0].split(" (", 1)[0] if short else w[0]
    if short:
        first = "source gap " + re.sub(r" with .*? at ", " at ", first.split(": ", 1)[1])
    return first + (f" (+{len(w) - 1} more)" if len(w) > 1 else "")


def describe(job) -> str:
    """One-line summary of what a job will do."""
    bits = []
    if isinstance(job, J.MergeJob):
        bits.append(f"Merge {len(job.clip_paths)} clips" if job.options.merge_mode != "trim"
                    else f"Remove repeats from {len(job.clip_paths)} clips")
    else:
        if job.segments:
            n = len(job.segments)
            bits.append(f"Trim {n} part{'s' if n > 1 else ''}")
        if not job.make_video:
            bits.append("No video")
        elif job.audio_tracks == []:
            bits.append("No audio")
        elif job.audio_tracks:
            bits.append("Tracks " + ",".join(str(t + 1) for t in job.audio_tracks))
    if job.mix_tracks:
        bits.append("Mixed audio")
    if job.compress and (isinstance(job, J.MergeJob) or job.make_video):
        e = EncodeSettings.from_dict(job.encode)
        auto = e.is_autoset
        e = e.applied()
        hw = "" if e.encoder == "software" else " " + e.encoder.upper()
        cap = f" {e.max_height}p" if e.max_height else ""
        if e.is_autoset:            # not chosen yet
            bits.append(f"Auto settings (give up {e.give_up}){' 10-bit' if e.ten_bit else ''}{hw}{cap}")
        else:
            bits.append(f"{codec_name(e)}{hw} {quality_text(e, short=True)}{' (auto settings)' if auto else ''}{cap}")
    elif isinstance(job, J.MergeJob):
        bits.append({"exact": "Frame-exact re-encode", "lossless": "Lossless only"}.get(
            job.options.merge_method, "Lossless when possible"))
    elif job.make_video:
        bits.append(("Smart cut" if job.options.smart_cut else "Frame-exact re-encode")
                    if job.segments and job.options.exact_cuts else "No re-encode")
    if isinstance(job, J.ClipJob) and job.rip_audio_tracks:
        bits.append(f"Rip {len(job.rip_audio_tracks)} track{'s' if len(job.rip_audio_tracks) > 1 else ''}"
                    f" ({job.options.rip_format})")
    if isinstance(job, J.ClipJob) and job.rip_video:
        bits.append("Rip video")
    return "  ·  ".join(bits)


def compression_line(job) -> str:
    """First line of the details panel: the compression a job will use, in full."""
    if not (job.compress and job.encode and (isinstance(job, J.MergeJob) or job.make_video)):
        if isinstance(job, J.ClipJob) and not job.make_video:
            how = "no video saved"
        elif isinstance(job, J.MergeJob):
            how = {"exact": "frame-exact re-encode", "lossless": "lossless only"}.get(
                job.options.merge_method, "lossless when possible")
        else:
            how = (("smart cut" if job.options.smart_cut else "frame-exact re-encode")
                   if job.segments and job.options.exact_cuts else "no re-encode")
        return f"Compression: none ({how})"
    e = EncodeSettings.from_dict(job.encode)
    a = e.applied()
    bits = []
    if e.is_autoset and not e.chosen:
        bits.append(f"Auto settings, measured (give up {e.give_up}): choosing codec and CRF…")
        bits.append("hardware " + a.encoder.upper() if a.encoder != "software" else "software")
    else:
        bits.append(f"{codec_name(a)} ({a.encoder_name})")
        bits.append(quality_text(a))
        if e.is_autoset:
            bits[-1] += f" (chosen by auto settings, give up {e.give_up})"
    bits.append(f"preset {a.preset}")
    bits.append("audio copied" if a.audio == "copy" else f"audio {a.audio} kbps")
    if a.max_height:
        bits.append(f"max {a.max_height}p")
    if a.max_fps:
        bits.append(f"max {a.max_fps:g} fps")
    if a.keyframe_interval:
        bits.append(f"keyframe every {a.keyframe_interval:g} s")
    if a.revert_if_larger:
        bits.append("keep original if larger")
    return "Compression: " + ", ".join(bits)


def default_name(job, settings) -> str:
    if isinstance(job, J.MergeJob):
        t = job.title_text
        if t.startswith("Session ") and job.options.merge_mode != "trim":
            return "Merged " + t[len("Session "):]
        return t.replace("Session ", "", 1) if t.startswith("Session ") else t
    return Path(job.source).stem + settings.name_suffix
