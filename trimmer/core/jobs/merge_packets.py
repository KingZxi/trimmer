"""A session whose replays share encoded frames: joined packet by packet, so no footage repeats
or is lost, and a stream copy is checked packet by packet against the sources."""

import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from .. import ffmpeg as ff
from .. import merge as mg
from .. import packets as pk
from ..encode import EncodeSettings, resolve_quality, video_codec_args, video_filters
from ..render import AudioPlan, _audio_codec_args, _track_meta_args, output_ext
from ..tag import tag_args
from ..util import format_hms_ms, sanitize_filename
from .checks import _gap_section, _piece_windows
from .model import EXACT_SETTINGS, NOT_COMPRESSED
from .output import _claim, _keep_smaller, _merge_rule, _merge_tag, _qlabel, _save, _trim_tag, _write_text


def _packet_session(job, ctx, session, pj: pk.PacketJoin, out_root, merged_name, folder_name,
                    enc, audio) -> List[Path]:
    opts = job.options
    first = pj.pieces[0].clip
    ext = output_ext(first.path, opts.container)
    srcs = [c.path for c in session.clips]
    outputs = []
    mark = len(ctx.warnings)
    with tempfile.TemporaryDirectory(prefix="trimmer_join_") as td:
        if opts.merge_mode == "trim":
            folder = out_root / folder_name
            folder.mkdir(parents=True, exist_ok=True)
            written = []
            for i, p in enumerate(pj.pieces):
                ctx.cancel.check()
                single = pk.PacketJoin([pk.Piece(p.clip, p.video, p.audio, p.stop, p.audio_stop, None)], [], True)
                kept_s = p.video.pts[p.stop] if p.stop is not None else p.clip.duration
                trim = _trim_tag(opts, [(0.0, kept_s)] if p.stop is not None else [])
                outputs.append(_packet_output(job, ctx, single, folder, p.clip.path.stem, ext, enc, audio, srcs,
                                              Path(td) / str(i), stage=f"Clip {i + 1} of {len(pj.pieces)}",
                                              trim=trim))
                written.append((p.clip, outputs[-1].name, kept_s))
            if opts.generate_clip_log:
                _write_text(folder / "info.txt", _packet_log(session, pj, written, trim_mode=True)
                            + _gap_section(ctx.warnings[mark:]))
            return outputs
        out = _packet_output(job, ctx, pj, out_root, merged_name, ext, enc, audio, srcs, Path(td),
                             stage="Merging")
        outputs.append(out)
        if opts.generate_clip_log:
            _write_text(out.with_name(f"{out.stem} - info.txt"), _packet_log(session, pj, None, False)
                        + _gap_section(ctx.warnings[mark:]))
        _log_splices_packets(ctx, pj)
        return outputs


def _packet_output(job, ctx, pj: pk.PacketJoin, folder: Path, stem: str, ext: str, enc, audio: AudioPlan,
                   srcs, tmpdir: Path, stage: str, trim: str = "") -> Path:
    """One output straight from an exact packet join: a stream copy whose every packet is checked
    against the sources, or one encode pass (when compressing or mixing tracks)."""
    opts = job.options
    tmpdir.mkdir(parents=True, exist_ok=True)
    vlist, alist = pk.write_concat_lists(pj, tmpdir)
    info = pj.pieces[0].clip.info
    n_avail = min(len(p.clip.info.audio) for p in pj.pieces)
    selected = audio.selected(n_avail)
    total = pj.total_duration
    date_src = pj.pieces[-1].clip.path
    inputs = ["-f", "concat", "-safe", "0", "-i", str(vlist)]
    a_in = 0
    if alist is not None and selected:
        inputs += ["-f", "concat", "-safe", "0", "-i", str(alist)]
        a_in = 1
    faststart = ["-movflags", "+faststart"] if ext in (".mp4", ".mov") else []

    def claim(extra: str) -> Path:
        return _claim(folder / f"{sanitize_filename(stem + opts.name_suffix)}{trim}{extra}{ext}", ctx, opts, srcs)

    def copy(extra: str = "") -> Path:
        args = inputs + ["-map", "0:v:0"] + sum((["-map", f"{a_in}:a:{t}"] for t in selected), []) + ["-c", "copy"]
        for i, t in enumerate(selected):
            tr = info.audio[t]
            if tr.title:
                args += [f"-metadata:s:a:{i}", f"title={tr.title}", f"-metadata:s:a:{i}", f"handler_name={tr.title}"]
        args += faststart + tag_args(_merge_tag(job, info.trimmer_tag, audio, n_avail))

        def nothing_lost(part):
            """Every source packet is in the output, once and in order."""
            problem = pk.compare(pj.expected_video(), pk.read_packets(part, "video").hashes, "video")
            if not problem and a_in and 0 in selected and selected[0] == 0:
                problem = pk.compare(pj.expected_audio(), pk.read_packets(part, "audio").hashes, "audio")
            if not problem:
                ctx.log("Nothing-lost check passed: every source frame is in the output exactly once.")
            return problem

        return _save(ctx, claim(extra),
                     lambda part: ff.run_ffmpeg(args + ["-y", str(part)], total, ctx.reporter(stage), ctx.cancel,
                                                output_path=part),
                     expected=total, n_audio=len(selected), date_source=date_src, check=nothing_lost,
                     gaps=(info.fps, _piece_windows(pj)), stage="Checking nothing was lost",
                     rejected="Merge failed the nothing-lost check")

    if enc is None and not audio.needs_filter:
        final = copy()
    else:
        # Encode straight from the exact packet join: one pass, frame exact.
        q, method = (resolve_quality(pj.pieces[0].clip.path, info, enc, ctx.cancel, ctx.log)
                     if enc is not None and enc.mode == "lossy" else (None, None))
        label = _qlabel(opts, enc, q, method) if enc is not None else ""
        e = enc or EncodeSettings.from_dict(EXACT_SETTINGS.to_dict())
        codec_args, out_tracks = _packet_encode_args(e, info, audio, selected, a_in, q, ext)
        args = inputs + codec_args + faststart + tag_args(_merge_tag(job, info.trimmer_tag, audio, n_avail, enc, q))

        def compressed(limit: Optional[int]) -> Path:
            return _save(ctx, claim(label),
                         lambda part: ff.run_ffmpeg(args + ["-y", str(part)], total, ctx.reporter(stage), ctx.cancel,
                                                    size_limit=limit, output_path=part),
                         expected=total, n_audio=len(out_tracks), date_source=date_src,
                         frames=(info.fps, _piece_windows(pj)))

        final = _keep_smaller(ctx, _merge_rule(enc, audio, pj.copy_bytes), compressed, lambda: copy(NOT_COMPRESSED),
                              predict=(enc, q, pj.pieces[0].clip.path, info, total))
    ctx.log(f"Saved {final.name}")
    return final


def _packet_encode_args(e: EncodeSettings, info, audio: AudioPlan, selected: List[int], a_in: int, q,
                        ext: str) -> Tuple[List[str], List[Optional[int]]]:
    """The maps, filters and codecs that encode a packet join (input 0: video, input a_in: audio).
    Returns them with the output's audio tracks (source track numbers; None for the mix)."""
    parts, amap = [], []
    vf = video_filters(e, info)
    vmap = "0:v:0"
    if vf:
        parts.append(f"[0:v:0]{','.join(vf)}[vout]")
        vmap = "[vout]"
    mix = [t for t in (audio.mix or []) if t in selected]
    out_tracks = []
    if len(mix) >= 2 and a_in:
        mix_in = []
        for t in mix:
            if audio.keep_originals:
                parts.append(f"[{a_in}:a:{t}]asplit=2[m{t}][k{t}]")
                mix_in.append(f"[m{t}]")
            else:
                mix_in.append(f"[{a_in}:a:{t}]")
        parts.append(f"{''.join(mix_in)}amix=inputs={len(mix)}:normalize=0:duration=longest,"
                     f"alimiter=limit=0.98:latency=1[mixed]")
        amap.append("[mixed]")
        out_tracks.append(None)
        for t in selected:
            if t in mix and not audio.keep_originals:
                continue
            amap.append(f"[k{t}]" if t in mix else f"{a_in}:a:{t}")
            out_tracks.append(t)
    elif a_in:
        amap = [f"{a_in}:a:{t}" for t in selected]
        out_tracks = list(selected)
    args = ["-filter_complex", ";".join(parts)] if parts else []
    args += ["-map", vmap] + sum((["-map", m] for m in amap), [])
    args += video_codec_args(e, info, quality=q if q is not None else 18, container=ext)
    args += _audio_codec_args(e, {"out_tracks": out_tracks, "audio_filtered": bool(parts and mix),
                                  "src_codecs": [info.audio[t].codec for t in selected]}, ext)
    args += _track_meta_args(info, out_tracks, "Mixed")
    return args, out_tracks


def _log_splices_packets(ctx, pj: pk.PacketJoin):
    t = 0.0
    for p, nxt in zip(pj.pieces, pj.pieces[1:]):
        t += p.duration or 0.0
        ctx.log(f"Join at {format_hms_ms(t)}: {p.clip.path.name} -> {nxt.clip.path.name}")


def _packet_log(session, pj: pk.PacketJoin, written, trim_mode: bool) -> str:
    lines = ["Settings", "--------", "Method: exact packet matching (no estimate, no safety margin needed)", "",
             "Clip boundaries", "---------------"]
    used = {id(p.clip): p for p in pj.pieces}
    names = {id(c): n for c, n, _ in (written or [])}
    # Length each piece takes up in the output, and where it starts (merge mode).
    lengths, starts, pos = [], [], 0.0
    for p in pj.pieces:
        if trim_mode:
            length = p.video.pts[p.stop] - p.clip.info.start_time if p.stop is not None else p.clip.duration
        else:
            length = p.duration if p.duration is not None else p.clip.duration
        lengths.append(length)
        starts.append(pos)
        pos += length
    index = {id(p.clip): k for k, p in enumerate(pj.pieces)}

    def end_line(c, own_k):
        """Where the clip's original, uncut end lands in the output."""
        hit = pj.locate_end(c)
        if hit is None:
            return None
        k, t = hit
        t = min(max(t, 0.0), lengths[k])        # rounding must not push it past the output
        if not trim_mode:
            return f"    Original clip ends in merged video : {format_hms_ms(starts[k] + t)}"
        if own_k is None:                       # no file of its own: count from where it starts
            first = pj.piece_at(pj.places[id(c)][0])
            own_k = k if first is None else first
        return mg.trim_end_line(t, names.get(id(pj.pieces[k].clip), pj.pieces[k].clip.path.name),
                                k - own_k, own=own_k is not None and id(c) in index)

    pos = 0.0
    for c in session.clips:
        p = used.get(id(c))
        if p is None:
            lines += [f"{c.path.name}  (every frame also in neighbouring clips; not used)"]
            el = end_line(c, None)
            lines += ([el] if el else []) + [""]
            continue
        end = p.video.pts[p.stop] if p.stop is not None else c.duration
        length = p.duration if p.duration is not None else c.duration
        if trim_mode:
            lines += [names.get(id(c), c.path.name),
                      f"    Span kept from original clip : {format_hms_ms(0)} - {format_hms_ms(end)}"
                      + ("" if p.stop is None else "  (the rest continues in the next file)")]
        else:
            lines += [c.path.name,
                      f"    Position in merged video : {format_hms_ms(pos)} - {format_hms_ms(pos + length)}",
                      f"    Span used from original  : {format_hms_ms(0)} - {format_hms_ms(end)}"]
        el = end_line(c, index[id(c)])
        lines += ([el] if el else []) + [""]
        pos += length
    return "\n".join(lines).rstrip() + "\n"
