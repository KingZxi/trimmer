"""Engine tests on synthetic clips. Run: python -m pytest tests -q"""

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TRIMMER_DATA_DIR", tempfile.mkdtemp(prefix="trimmer_data_"))

from trimmer.core import ffmpeg as ff                     # noqa: E402
from trimmer.core import packets as pk                    # noqa: E402
from trimmer.core import peek                              # noqa: E402
from trimmer.core.encode import EncodeSettings            # noqa: E402
from trimmer.core.jobs import (ClipJob, JobContext, JobOptions, MergeJob,  # noqa: E402
                                 run_job)
from tests import fixtures as fx                            # noqa: E402

WORK = Path(os.environ.get("TRIMMER_TEST_DIR", tempfile.gettempdir())) / "trimmer_tests"


@pytest.fixture(scope="session")
def work():
    WORK.mkdir(parents=True, exist_ok=True)
    return WORK


def fresh(work, name):
    d = work / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


def ctx(logs=None):
    logs = logs if logs is not None else []
    return JobContext(log=logs.append), logs


def opts(out, **kw):
    o = JobOptions(output_dir=str(out), conflict_policy="rename")
    for k, v in kw.items():
        setattr(o, k, v)
    return o


# ---------------------------------------------------------------- packets

def hms(text):
    h, m, sec = text.split(":") if text.count(":") == 2 else ("0", *text.split(":"))
    return int(h) * 3600 + int(m) * 60 + float(sec)


def clip_ends(info_text):
    """{start second from the clip's file name: its 'Original clip ends' line}."""
    body = info_text.split("Clip boundaries\n---------------\n", 1)[1]
    out = {}
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        sec = int(re.search(r"(\d\d)(?: \(trimmed [^)]*\))?\.mp4", lines[0]).group(1))
        out[sec] = next(l for l in lines if l.lstrip().startswith("Original clip ends"))
    return out


def end_time(line):
    return hms(line.split(" : ")[1].split()[0])


def test_packet_merge_is_exact(work):
    clips = fx.shared_replays(work / "shared")
    out = fresh(work, "out_packet_merge")
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips], options=opts(out, generate_clip_log=True)), c)
    assert r.status == "done", logs
    vids = sorted(out.glob("*.mp4"))
    assert len(vids) == 2, [p.name for p in out.iterdir()]   # main session + the separate single clip
    assert any("Nothing-lost check passed" in l for l in logs), logs
    assert any("every frame already appears" in l for l in logs), logs   # 14-30 clip dropped
    main = max(vids, key=lambda p: p.stat().st_size)
    info = ff.probe(main, use_cache=False)
    assert abs(info.duration - 48.0) < 0.15, info.duration
    assert len(info.audio) == 2 and info.audio[0].title == "Mic"
    assert not list(out.glob("*.part*"))
    info_file = next(out.glob("Merged*- info.txt"))
    ends = {k: end_time(v) for k, v in clip_ends(info_file.read_text(encoding="utf-8")).items()}
    # Clips are named by their start second: 0-20, 12-32, 14-30 (dropped), 28-48.
    # A clip's duration runs ~0.1 s past its nominal length (the stream copy keeps a
    # couple of extra frames), so allow that on top of one frame.
    tol = 0.2
    assert abs(ends[0] - 20.0) < tol and abs(ends[12] - 32.0) < tol, ends
    assert abs(ends[14] - 30.0) < tol, ends                # a dropped clip is placed too
    assert abs(ends[28] - 48.0) < tol, ends


def test_packet_merge_matches_continuous_source(work):
    """The merged video's frames equal the continuous 'OBS stream' 0-48 s."""
    out = work / "out_packet_merge"
    main = max(out.glob("Merged*.mp4"), key=lambda p: p.stat().st_size)
    src = pk.read_packets(work / "obs_stream.mp4", "video").hashes
    got = pk.read_packets(main, "video").hashes
    assert got == src[:len(got)]
    assert 48 * 30 <= len(got) <= 48 * 30 + 4


def test_packet_trim_mode(work):
    clips = fx.shared_replays(work / "shared")
    out = fresh(work, "out_packet_trim")
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips[:4]],
                         options=opts(out, merge_mode="trim", generate_clip_log=True)), c)
    assert r.status == "done", logs
    folder = next(out.glob("Session *"))
    files = sorted(folder.glob("*.mp4"))
    assert len(files) == 3
    # The first two lose their ends to the next replay; the last is whole.
    assert files[0].name.endswith(" (trimmed 0m00s-0m12s).mp4"), files
    assert files[1].name.endswith(" (trimmed 0m00s-0m16s).mp4"), files
    assert "(" not in files[2].name, files
    total = sum(ff.probe(f, use_cache=False).duration for f in files)
    assert abs(total - 48.0) < 0.35, total
    ends = clip_ends((folder / "info.txt").read_text(encoding="utf-8"))
    # B-frame start offsets and the extra frames kept by the stream copy add ~0.15 s
    # to the nominal positions.
    tol = 0.2
    # Clip 0-20 is cut where 12-32 begins, so its end is 8 s into the next file.
    assert f"into {files[1].name} (1 file later)" in ends[0], ends
    assert abs(end_time(ends[0]) - 8.0) < tol, ends
    # Clip 12-32 is cut where 28-48 begins; its end is 4 s into that file.
    assert f"into {files[2].name} (1 file later)" in ends[12], ends
    assert abs(end_time(ends[12]) - 4.0) < tol, ends
    # The contained clip 14-30 writes no file. It starts inside the 12-32 file, which is cut
    # at 28 s, so its end (30 s) is 2 s into the next file.
    assert ends[14].rstrip().endswith(f"into {files[2].name}"), ends     # no "files later" without a file
    assert abs(end_time(ends[14]) - 2.0) < tol, ends
    # The last clip ends in its own file.
    assert ends[28].endswith("in this file"), ends
    assert abs(end_time(ends[28]) - 20.0) < tol, ends


def test_packet_merge_with_compress_single_pass(work):
    clips = fx.shared_replays(work / "shared")
    out = fresh(work, "out_packet_compress")
    enc = EncodeSettings(codec="h264", quality=28, preset="ultrafast").to_dict()
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips[:4]], compress=True, encode=enc, options=opts(out)), c)
    assert r.status == "done", logs
    f = next(out.glob("Merged*.mp4"))
    assert "(LOSSY), (CRF28, H264, ultrafast)" in f.name
    assert abs(ff.probe(f, use_cache=False).duration - 48.0) < 0.3


def test_compressed_merge_never_bigger_than_plain_merge(work):
    clips = fx.shared_replays(work / "shared")
    out = fresh(work, "out_packet_bigger")
    # Lossless is far bigger than the source: the encode stops and a plain merge is saved instead.
    enc = EncodeSettings(mode="lossless", codec="h264", preset="ultrafast", revert_if_larger=True).to_dict()
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips[:4]], compress=True, encode=enc, options=opts(out)), c)
    assert r.status == "done", logs
    f = next(out.glob("Merged*.mp4"))
    assert f.name.endswith("(NOT COMPRESSED).mp4") and "LOSSLESS" not in f.name, f.name
    assert any("larger than a plain merge" in l for l in logs), logs
    assert any("Nothing-lost check passed" in l for l in logs), logs
    assert not list(out.glob("*.part*"))

    # A lossy encode the sample already shows is bigger never runs at all.
    out2 = fresh(work, "out_packet_predicted")
    enc = EncodeSettings(codec="h264", quality=1, preset="ultrafast", revert_if_larger=True).to_dict()
    c, logs = ctx()
    run_job(MergeJob([str(p) for p in clips[:4]], compress=True, encode=enc, options=opts(out2)), c)
    assert any("Predicted size" in l for l in logs), logs
    assert next(out2.glob("Merged*.mp4")).name.endswith("(NOT COMPRESSED).mp4")

    # One that really is smaller is kept compressed.
    out3 = fresh(work, "out_packet_smaller")
    enc = EncodeSettings(codec="h264", quality=40, preset="medium", revert_if_larger=True).to_dict()
    c, logs = ctx()
    run_job(MergeJob([str(p) for p in clips[:4]], compress=True, encode=enc, options=opts(out3)), c)
    assert "(CRF40, H264, medium)" in next(out3.glob("Merged*.mp4")).name, logs


def test_fallback_merge_never_bigger_than_plain_merge(work):
    clips = fx.independent_replays(work / "indep", wrong_offset=1)
    enc = EncodeSettings(mode="lossless", codec="h264", preset="ultrafast", revert_if_larger=True).to_dict()
    out = fresh(work, "out_fallback_bigger")
    c, logs = ctx()
    # "Lossless" so a plain merge is possible here (its keyframe cuts repeat 1.75 s).
    r = run_job(MergeJob([str(p) for p in clips], compress=True, encode=enc,
                         options=opts(out, merge_method="lossless")), c)
    assert r.status == "done", logs
    f = next(out.glob("Merged*.mp4"))
    assert f.name.endswith("(NOT COMPRESSED).mp4"), f.name
    assert 47.9 <= ff.probe(f, use_cache=False).duration <= 52.5
    out2 = fresh(work, "out_fallback_trim_bigger")
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips], compress=True, encode=enc,
                         options=opts(out2, merge_mode="trim")), c)
    assert r.status == "done", logs
    files = sorted(next(out2.glob("Session *")).glob("*.mp4"))
    assert len(files) == 3 and all("(NOT COMPRESSED)" in f.name for f in files), files
    assert not list(out2.rglob("*.part*"))


# ---------------------------------------------------------------- replay rips

def n_streams(path):
    i = ff.probe(path, use_cache=False)
    return len(i.audio), i.has_video, i.duration


def test_merge_rips_without_the_merged_video(work):
    clips = fx.shared_replays(work / "shared")
    out = fresh(work, "out_merge_rips")
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips[:4]], make_video=False, rip_audio_tracks=[1], rip_video=True,
                         options=opts(out, rip_format="flac", generate_clip_log=True)), c)
    assert r.status == "done", logs
    assert not list(out.glob("*.mp4")) or all("video only" in f.name for f in out.glob("*.mp4"))
    info_file = list(out.glob("Merged * - info.txt"))
    assert len(info_file) == 1, sorted(out.iterdir())     # written even with no merged video
    text = info_file[0].read_text(encoding="utf-8")
    assert text.startswith("No video was saved")
    assert abs(end_time(clip_ends(text)[28]) - 48.0) < 0.2
    audio = list(out.glob("Merged * - Track 2 (Desktop).flac"))
    assert len(audio) == 1, sorted(out.iterdir())
    na, has_v, dur = n_streams(audio[0])
    assert (na, has_v) == (1, False) and abs(dur - 48.0) < 0.2, dur
    vids = list(out.glob("Merged * - video only.mp4"))
    assert len(vids) == 1
    na, has_v, dur = n_streams(vids[0])
    assert (na, has_v) == (0, True) and abs(dur - 48.0) < 0.35, dur
    assert len(r.outputs) == 2 and not list(out.glob("*.part*"))


def test_trim_rips_without_video_write_the_info_file(work):
    clips = fx.shared_replays(work / "shared")
    out = fresh(work, "out_trim_rips")
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips[:4]], make_video=False, rip_audio_tracks=[0],
                         options=opts(out, merge_mode="trim", generate_clip_log=True)), c)
    assert r.status == "done", logs
    folder = next(out.glob("Session *"))
    assert len(list(folder.glob("* - Track 1*"))) == 3, sorted(folder.iterdir())
    assert not list(folder.glob("*.mp4"))
    text = (folder / "info.txt").read_text(encoding="utf-8")
    assert text.startswith("No video was saved")
    assert len(clip_ends(text)) == 4


def test_merge_rips_straight_from_the_real_output(work):
    clips = fx.shared_replays(work / "shared")
    out = fresh(work, "out_merge_rips_direct")
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips[:4]], rip_audio_tracks=[0],
                         options=opts(out, rip_format="wav")), c)
    assert r.status == "done", logs
    assert not any("Preparing the tracks" in l for l in logs)
    merged = next(out.glob("Merged*[0-9].mp4"))
    assert n_streams(merged)[0] == 2
    assert len(list(out.glob("Merged * - Track 1 (Mic).wav"))) == 1
    assert len(r.outputs) == 2


def test_trim_mode_rips_per_piece(work):
    clips = fx.shared_replays(work / "shared")
    out = fresh(work, "out_trim_rips")
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips[:4]], audio_tracks=[0], rip_audio_tracks=[0],
                         options=opts(out, merge_mode="trim", rip_format="flac")), c)
    assert r.status == "done", logs
    folder = next(out.glob("Session *"))
    videos = sorted(f for f in folder.glob("*.mp4"))
    rips = sorted(folder.glob("* - Track 1 (Mic).flac"))
    assert len(videos) == 3 and len(rips) == 3, sorted(folder.iterdir())
    assert all(n_streams(v)[0] == 1 for v in videos)
    assert sorted(f.name.split(" - Track")[0] for f in rips) == sorted(v.stem for v in videos)
    assert sum("(trimmed 0m" in v.name for v in videos) == 2, videos
    assert abs(sum(n_streams(f)[2] for f in rips) - 48.0) < 0.5
    assert len(r.outputs) == 6


def test_single_clip_session_passes_rips_through(work):
    clips = fx.shared_replays(work / "shared")
    out = fresh(work, "out_single_rips")
    c, logs = ctx()
    r = run_job(MergeJob([str(clips[4])], make_video=False, rip_audio_tracks=[1], rip_video=True,
                         options=opts(out, rip_format="flac")), c)
    assert r.status == "done", logs
    names = sorted(Path(p).name for p in r.outputs)
    assert len(names) == 2 and any("Track 2 (Desktop).flac" in n for n in names) \
        and any(n.endswith("video only.mp4") for n in names), names


# ---------------------------------------------------------------- fallback

def test_fallback_merge_uses_audio_alignment(work):
    clips = fx.independent_replays(work / "indep", wrong_offset=1)
    out = fresh(work, "out_fallback")
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips], options=opts(out, generate_clip_log=True)), c)
    assert r.status == "done", logs
    assert any("audio and timestamp method" in l for l in logs), logs
    assert any("audio-aligned" in l for l in logs), logs
    f = next(out.glob("Merged*.mp4"))
    d = ff.probe(f, use_cache=False).duration
    # True footage is 48 s; safety margins may repeat up to ~0.5 s per join, never lose.
    assert 47.9 <= d <= 49.5, d
    ends = clip_ends(next(out.glob("Merged*- info.txt")).read_text(encoding="utf-8"))
    assert len(ends) == 3 and all("in merged video" in l for l in ends.values()), ends
    assert abs(end_time(ends[0]) - 20.0) < 1.5 and abs(end_time(ends[13]) - 32.0) < 1.5, ends
    assert abs(end_time(ends[28]) - d) < 1.5, ends


def test_fallback_trim_mode(work):
    clips = fx.independent_replays(work / "indep", wrong_offset=1)
    out = fresh(work, "out_fallback_trim")
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips], options=opts(out, merge_mode="trim", generate_clip_log=True)), c)
    assert r.status == "done", logs
    folder = next(out.glob("Session *"))
    files = sorted(folder.glob("*.mp4"))
    assert len(files) == 3
    # The fallback cuts later replays at their start; the first is whole.
    assert "(" not in files[0].name and all(" (trimmed 0m" in f.name for f in files[1:]), files
    ends = clip_ends((folder / "info.txt").read_text(encoding="utf-8"))
    assert len(ends) == 3 and all(l.endswith("in this file") for l in ends.values()), ends
    assert abs(end_time(ends[0]) - 20.0) < 0.1, ends


# ---------------------------------------------------------------- clip jobs

def test_lossless_trim_never_loses_start(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_trim")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(5.3, 12.0)], options=opts(out)), c)
    assert r.status == "done", logs
    f = next(out.glob("* (trimmed 0m04s-0m12s).mp4"))    # named by where the copy really starts
    d = ff.probe(f, use_cache=False).duration
    assert 6.7 <= d <= 8.8, d            # snapped back to the keyframe at or before 5.3 s
    assert any("moved back" in l for l in logs)


def test_cut_out_middle_and_audio_options(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_multi")
    c, logs = ctx()
    job = ClipJob(str(src), segments=[(0, 4), (10, 16)], compress=True,
                  encode=EncodeSettings(codec="h264", quality=30, preset="ultrafast").to_dict(),
                  mix_tracks=[0, 1], keep_mixed_originals=True, rip_audio_tracks=[0, 1],
                  options=opts(out, rip_format="wav"))
    r = run_job(job, c)
    assert r.status == "done", logs
    vid = next(out.glob("*.mp4"))
    assert " (trimmed 0m00s-0m04s, 0m10s-0m16s) (mixed audio) (LOSSY), (CRF30" in vid.name, vid.name
    info = ff.probe(vid, use_cache=False)
    assert abs(info.duration - 10.0) < 0.15
    assert len(info.audio) == 3 and info.audio[0].title == "Mixed"
    wavs = sorted(out.glob("*.wav"))
    assert len(wavs) == 2 and "(Mic)" in wavs[0].name
    assert wavs[0].name.endswith(" (trimmed 0m00s-0m04s, 0m10s-0m16s) - Track 1 (Mic).wav"), wavs[0].name
    assert abs(ff.probe(wavs[0], use_cache=False).duration - 10.0) < 0.15


def test_video_only_and_no_audio(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_vonly")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), make_video=False, rip_video=True, options=opts(out)), c)
    assert r.status == "done", logs
    f = next(out.glob("*video only*"))
    assert ff.probe(f, use_cache=False).audio == []


def test_target_size(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_target")
    enc = EncodeSettings(mode="target_size", codec="h264", target_size_mb=1.0, preset="ultrafast",
                         audio="64").to_dict()
    c, logs = ctx()
    r = run_job(ClipJob(str(src), compress=True, encode=enc, options=opts(out)), c)
    assert r.status == "done", logs
    f = next(out.glob("*.mp4"))
    assert f.stat().st_size <= 1.1 * 1024 * 1024, f.stat().st_size


def test_revert_if_larger(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_revert")
    enc = EncodeSettings(mode="lossless", codec="h264", preset="ultrafast", revert_if_larger=True).to_dict()
    c, logs = ctx()
    r = run_job(ClipJob(str(src), compress=True, encode=enc, options=opts(out)), c)
    assert r.status == "reverted", logs
    assert list(out.glob("*(NOT COMPRESSED).mp4"))
    assert not list(out.glob("*.part*"))


def test_auto2_and_auto3(work):
    src = fx.single_clip(work / "single")
    for q in ("auto2", "auto3"):
        out = fresh(work, f"out_{q}")
        enc = EncodeSettings(codec="h264", quality=q, preset="ultrafast").to_dict()
        c, logs = ctx()
        r = run_job(ClipJob(str(src), compress=True, encode=enc, options=opts(out)), c)
        assert r.status == "done", logs
        f = next(out.glob("*.mp4"))
        assert re.search(rf"CRF\d+ {q}", f.name), f.name


def test_conflict_and_delete_original(work):
    src_dir = fresh(work, "del_src")
    src = fx.single_clip(src_dir)
    out = fresh(work, "out_del")
    c, logs = ctx()
    run_job(ClipJob(str(src), segments=[(0, 5)], options=opts(out)), c)
    r = run_job(ClipJob(str(src), segments=[(0, 5)], options=opts(out, delete_originals=True,
                                                                  permanent_delete=True)), c)
    assert r.status == "done", logs
    assert len(list(out.glob("*.mp4"))) == 2      # renamed, not overwritten
    assert not src.exists()


def test_cancel_leaves_no_partial(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_cancel")
    enc = EncodeSettings(codec="h265", quality=20, preset="slow").to_dict()
    from trimmer.core.ffmpeg import Cancelled
    import threading
    c, logs = ctx()
    threading.Timer(1.5, c.cancel.cancel).start()
    with pytest.raises(Cancelled):
        run_job(ClipJob(str(src), compress=True, encode=enc, options=opts(out)), c)
    assert list(out.iterdir()) == []


def test_no_network_code():
    banned = re.compile(r"\b(import socket|from socket|urllib|http\.client|requests|QtNetwork|"
                        r"QNetwork|websocket|telemetry)\b")
    for f in [*(ROOT / "trimmer").rglob("*.py"), ROOT / "trimmer.py", ROOT / "Trimmer.pyw"]:
        text = f.read_text(encoding="utf-8")
        assert not banned.search(text), f"network-related code in {f}"


def test_copy_cut_out_middle_keeps_sync(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_copy_multi")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(0, 4), (10.4, 16)], options=opts(out)), c)
    assert r.status == "done", logs
    f = next(out.glob("*.mp4"))
    o = ff.run_quiet([ff.ffprobe_bin(), "-v", "error", "-show_entries", "stream=codec_type,duration",
                      "-of", "csv=p=0", str(f)]).stdout.split()
    durs = [float(x.split(",")[1]) for x in o]
    assert max(durs) - min(durs) < 0.1, o           # audio and video stay the same length
    assert 9.9 <= durs[0] <= 10.5, durs              # second part snapped back to the keyframe at 10 s


def test_custom_output_names(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_names")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(2, 8)], rip_audio_tracks=[0], output_name="Big play.mp4",
                        options=opts(out, name_suffix="_x", rip_format="wav")), c)
    assert r.status == "done", logs
    names = sorted(p.name for p in out.iterdir())
    # A stream copy starts on the keyframe at 0.08 s; the name and the rip follow it.
    assert names == ["Big play (trimmed 0m00s-0m08s) - Track 1 (Mic).wav",
                     "Big play (trimmed 0m00s-0m08s).mp4"], names
    # Without the range setting the tag stays plain "(trimmed, ...)".
    r = run_job(ClipJob(str(src), segments=[(2, 8)], audio_tracks=[],
                        options=opts(out, add_trim_range=False)), c)
    assert r.status == "done", logs
    assert Path(r.outputs[0]).name == f"{src.stem} (trimmed, no audio).mp4", r.outputs

    clips = fx.shared_replays(work / "shared")
    r = run_job(MergeJob([str(p) for p in clips[:4]], output_name="Clutch round",
                         options=opts(out, name_suffix="_x")), c)
    assert r.status == "done", logs
    assert (out / "Clutch round.mp4").exists()
    r = run_job(MergeJob([str(p) for p in clips[:4]], output_name="Round folder",
                         options=opts(out, merge_mode="trim")), c)
    assert r.status == "done", logs
    assert (out / "Round folder").is_dir() and len(list((out / "Round folder").glob("*.mp4"))) == 3


def test_pause_resume_mid_encode_and_cancel_while_paused(work):
    import threading
    import time
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_pause")
    enc = EncodeSettings(codec="h265", quality=20, preset="slow", revert_if_larger=False).to_dict()
    c, logs = ctx()
    seen = {}

    def progress(stage, p):
        if p.size:
            seen["size"] = p.size
    c.progress = progress
    result = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "r", run_job(ClipJob(str(src), compress=True, encode=enc, options=opts(out, add_quality_label=False)), c)))
    t.start()
    deadline = time.time() + 30
    while time.time() < deadline and not seen.get("size"):
        time.sleep(0.05)
    c.cancel.pause()
    time.sleep(0.5)
    parts = list(out.glob("*.part.*"))
    assert parts, "expected an in-progress file"
    size_a = parts[0].stat().st_size
    time.sleep(1.5)
    assert parts[0].stat().st_size == size_a          # frozen: nothing written while paused
    assert t.is_alive()
    c.cancel.resume()
    t.join(120)
    assert result["r"].status == "done", logs
    assert [p.name for p in out.iterdir()] == ["Gameplay clip.mp4"]   # no compression details in name

    # Cancelling while paused still stops cleanly and leaves nothing behind.
    out2 = fresh(work, "out_pause_cancel")
    c2, _ = ctx()
    errors = []

    def run2():
        try:
            run_job(ClipJob(str(src), compress=True, encode=enc, options=opts(out2)), c2)
        except Exception as e:      # Cancelled expected
            errors.append(type(e).__name__)
    t2 = threading.Thread(target=run2)
    t2.start()
    time.sleep(1.5)
    c2.cancel.pause()
    time.sleep(0.5)
    c2.cancel.cancel()
    t2.join(30)
    assert not t2.is_alive() and errors == ["Cancelled"]
    assert list(out2.iterdir()) == []


# ---------------------------------------------------------------- smart cut

def _decoded(path, ss=None, t=None):
    import subprocess
    args = ["ffmpeg", "-v", "error"] + (["-ss", f"{ss}"] if ss is not None else []) + ["-i", str(path)]
    args += (["-t", f"{t}"] if t is not None else []) + ["-map", "0:v:0", "-fps_mode", "passthrough",
                                                         "-f", "framemd5", "-"]
    out = subprocess.run(args, capture_output=True, text=True)
    assert out.stderr.strip() == "", out.stderr
    return [ln.rsplit(",", 1)[-1].strip() for ln in out.stdout.splitlines() if ln and not ln.startswith("#")]


def test_smart_cut_is_frame_exact_and_keeps_original_frames(work):
    src = fx.single_clip(work / "single")          # 2 s GOP, B-frames
    out = fresh(work, "out_smart")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(5.3, 12.7)], options=opts(out, exact_cuts=True)), c)
    assert r.status == "done", logs
    assert any(l.startswith("Smart cut: re-encoding") for l in logs), logs
    f = next(out.glob("* (trimmed 0m05s-0m13s).mp4"))
    info = ff.probe(f, use_cache=False)
    assert abs(info.duration - 7.4) < 0.02, info.duration
    assert [a.title for a in info.audio] == ["Mic", "Desktop"]
    # Between the first and last keyframe inside the cut (6.08-12.08 s; the
    # clip's video starts at 0.08 s) the pictures are the source's own.
    got, full = _decoded(f), _decoded(src)
    assert len(got) == 222                         # 7.4 s at 30 fps, nothing extra
    first = full.index(got[40]) - 40               # source frame where the output starts
    assert first == 157                            # 5.313 s: the first frame at or after 5.3 s
    assert got[23:23 + 180] == full[180:360]
    # And the copied packets themselves are the original bytes.
    a, b = pk.read_packets(src, "video"), pk.read_packets(f, "video")
    assert sum(1 for h in b.hashes if h in set(a.hashes)) >= 170


def test_smart_cut_several_parts_with_mix(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_smart_multi")
    c, logs = ctx()
    job = ClipJob(str(src), segments=[(1.1, 5.5), (9.25, None)], mix_tracks=[0, 1], volumes={"1": 0.5},
                  options=opts(out, exact_cuts=True))
    r = run_job(job, c)
    assert r.status == "done", logs
    assert not any("re-encoding the trim instead" in l for l in logs), logs
    info = ff.probe(next(out.glob("*.mp4")), use_cache=False)
    expected = 4.4 + ff.probe(src).duration - 9.25
    assert abs(info.duration - expected) < 0.05, (info.duration, expected)
    assert [a.title for a in info.audio] == ["Mixed", "Mic", "Desktop"]


def test_smart_cut_hevc(work):
    import subprocess
    d = fresh(work, "smart_hevc")
    src = d / "hevc clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(fx.master(work)), "-t", "12", "-map", "0",
                    "-c:v", "libx265", "-x265-params", "log-level=error:keyint=60:min-keyint=60:open-gop=0",
                    "-tag:v", "hvc1", "-c:a", "aac", str(src)], check=True)
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(2.5, 9.9)], options=opts(d / "o", exact_cuts=True)), c)
    assert r.status == "done", logs
    assert not any("re-encoding the trim instead" in l for l in logs), logs
    f = next((d / "o").glob("*.mp4"))
    info = ff.probe(f, use_cache=False)
    assert info.video_codec == "hevc" and abs(info.duration - 7.4) < 0.02
    assert _decoded(f)[45:45 + 50] == _decoded(src, 4.0, 2.0)[:50]


def test_smart_cut_falls_back_for_other_codecs(work):
    import subprocess
    d = fresh(work, "smart_mpeg4")
    src = d / "old clip.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(fx.master(work)), "-t", "8", "-map", "0",
                    "-c:v", "mpeg4", "-g", "60", "-c:a", "aac", str(src)], check=True)
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(1.3, 6.0)], options=opts(d / "o", exact_cuts=True)), c)
    assert r.status == "done", logs
    assert any("Smart cut not possible" in l for l in logs), logs
    assert abs(ff.probe(next((d / "o").glob("*.mkv")), use_cache=False).duration - 4.7) < 0.1


def test_peek_waveform_and_thumbnails(work):
    src = fx.single_clip(work / "single")
    w = peek.waveform(src, buckets=200)
    assert len(w) == 200 and 0 <= w.min() and w.max() <= 1 and w.max() > 0.05
    assert peek.waveform(src, buckets=200) is w                   # cached
    one = peek.waveform(src, buckets=200, track=0)
    assert len(one) == 200 and one.max() <= 1
    info = ff.probe(src)
    th = peek.thumbnails(src, info.duration, 6, height=40)
    assert len(th) == 6
    assert [round(t, 1) for t, _ in th] == sorted(round(t, 1) for t, _ in th)
    assert all(0 < t < info.duration and b[:2] == b"\xff\xd8" for t, b in th)   # JPEGs
    assert peek.thumbnails(src, 0, 6) == []


def test_peek_silent_clip_has_no_waveform(work):
    d = work / "silent"
    d.mkdir(exist_ok=True)
    out = d / "silent.mp4"
    if not out.exists():
        fx.run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=3", "-c:v", "libx264", str(out))
    assert len(peek.waveform(out)) == 0
    assert len(peek.thumbnails(out, 3.0, 3)) == 3


# ---------------------------------------------------------------- space saved

@pytest.fixture
def stats(tmp_path, monkeypatch):
    """A private stats.json so the totals start at zero and tests stay independent."""
    monkeypatch.setenv("TRIMMER_DATA_DIR", str(tmp_path / "data"))
    from trimmer.core import savings
    return savings


def _compress_job(src, out, **kw):
    enc = EncodeSettings(codec="h264", quality=30, preset="veryfast").to_dict()
    return ClipJob(str(src), compress=True, encode=enc, options=opts(out, **kw))


def test_savings_counts_deleted_originals(work, stats):
    src = fx.single_clip(fresh(work, "sv_src1"))
    size = src.stat().st_size
    c, logs = ctx()
    r = run_job(_compress_job(src, fresh(work, "sv_out1"), delete_originals=True, permanent_delete=True), c)
    assert r.status == "done", logs
    assert not src.exists() and r.deleted_bytes == size and r.source_count == 1
    s = stats.record(r)
    assert s.saved_bytes == size - r.out_bytes == s.reduction_bytes
    assert (s.jobs, s.clips) == (1, 1)
    assert stats.load() == s
    assert "Space saved:" in stats.summary_text(s)


def test_savings_kept_originals_only_reduction_unless_enabled(work, stats):
    src = fx.single_clip(fresh(work, "sv_src2"))
    c, logs = ctx()
    r = run_job(_compress_job(src, fresh(work, "sv_out2")), c)
    assert r.status == "done" and src.exists() and r.deleted_bytes == 0, logs
    diff = r.in_bytes - r.out_bytes
    s = stats.record(r)
    assert (s.saved_bytes, s.reduction_bytes, s.jobs, s.clips) == (0, diff, 1, 1)
    s = stats.record(r, count_kept=True)
    assert (s.saved_bytes, s.reduction_bytes, s.jobs, s.clips) == (diff, 2 * diff, 2, 2)


def test_savings_ignores_failed_cancelled_skipped_and_counts_merge(work, stats):
    from trimmer.core.jobs import JobResult
    for status in ("failed", "skipped", "cancelled"):
        stats.record(JobResult(status=status, in_bytes=100, out_bytes=10, source_count=1), True)
    assert stats.load().jobs == 0 and stats.load().saved_bytes == 0
    missing = run_job(ClipJob(str(work / "nope.mp4"), options=opts(work)), ctx()[0])
    assert missing.status == "failed"
    stats.record(missing, True)
    assert stats.load().jobs == 0
    # Negative savings (output bigger than the deleted source) are kept honestly.
    s = stats.record(JobResult(status="reverted", in_bytes=100, out_bytes=130, deleted_bytes=100, source_count=3))
    assert (s.saved_bytes, s.reduction_bytes, s.clips) == (-30, -30, 3)
    assert "larger" in stats.summary_text(s)


def test_savings_recovers_from_corrupt_file_and_resets(stats):
    from trimmer.core.jobs import JobResult
    stats.path().parent.mkdir(parents=True, exist_ok=True)
    for junk in ("{not json", "[1, 2]", '{"saved_bytes": "lots", "jobs": -4, "since": 7}'):
        stats.path().write_text(junk, encoding="utf-8")
        s = stats.load()
        assert (s.saved_bytes, s.jobs, s.clips) == (0, 0, 0)
    s = stats.record(JobResult(in_bytes=50, out_bytes=20, source_count=2))
    assert (s.reduction_bytes, s.jobs) == (30, 1)
    assert stats.reset().jobs == 0 and stats.load().reduction_bytes == 0


def test_savings_set_by_hand(stats):
    stats.reset()
    s = stats.set_saved(5 * 1024 ** 3)
    assert s.saved_bytes == 5 * 1024 ** 3 and stats.load().saved_bytes == 5 * 1024 ** 3
    assert "5.0 GB" in stats.summary_text()


# ------------------------------------------------------------ data safety

def _pcm(path, track=0):
    import subprocess
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:a:{track}", "-ac", "1",
                          "-ar", "48000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(float)


def _rip_offset(video, rip, track):
    """Seconds into the video's own audio track where the rip starts."""
    import numpy as np
    a, b = _pcm(video, track), _pcm(rip)
    n = 48000 * 2
    return max(range(0, 48000), key=lambda k: float(np.dot(a[k:k + n], b[:n]))) / 48000


@pytest.mark.parametrize("exact", [False, True])
def test_clip_rips_line_up_with_the_saved_video(work, exact):
    src = fx.single_clip(work / "single")
    out = fresh(work, f"out_rip_sync_{exact}")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(5.5, 12.0)], rip_audio_tracks=[0, 1],
                        options=opts(out, exact_cuts=exact)), c)
    assert r.status == "done", logs
    video = next(out.glob("*.mp4"))
    rips = sorted(out.glob("*.m4a"))
    assert len(rips) == 2, logs
    for t, rip in enumerate(rips):
        assert _rip_offset(video, rip, t) == 0.0, (rip.name, logs)
        assert abs(ff.probe(rip, use_cache=False).duration - ff.probe(video, use_cache=False).duration) < 0.1
    if not exact:
        # The copy starts on the keyframe at 4.08 s, and the names say so.
        assert video.name == "Gameplay clip (trimmed 0m04s-0m12s).mp4", video.name
        assert all(p.name.startswith("Gameplay clip (trimmed 0m04s-0m12s) - Track") for p in rips)


def test_rips_without_a_video_keep_the_exact_marks(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_rip_only")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(5.5, 12.0)], make_video=False, rip_audio_tracks=[0],
                        options=opts(out)), c)
    assert r.status == "done", logs
    rip = next(out.glob("*.m4a"))
    assert abs(ff.probe(rip, use_cache=False).duration - 6.5) < 0.05


def _first_audio_pts(path: Path) -> float:
    out = ff.run_quiet([ff.ffprobe_bin(), "-v", "error", "-select_streams", "a:0", "-show_entries",
                        "packet=pts_time", "-of", "csv=p=0", "-read_intervals", "%+#1", str(path)])
    return float(out.stdout.split()[0].strip(","))


def test_compressed_trim_and_rip_have_no_audio_before_the_cut(work):
    """A trim starting mid-GOP with the audio copied: the packets the demuxer
    reads from the keyframe before the cut must not be kept (hidden behind an
    edit list, players that ignore it played the audio that much late)."""
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_preroll")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(5.5, None)], compress=True, rip_audio_tracks=[0],
                        encode=EncodeSettings(codec="h264", quality=30, preset="ultrafast").to_dict(),
                        options=opts(out)), c)
    assert r.status == "done", logs
    video = next(out.glob("*.mp4"))
    rip = next(out.glob("*.m4a"))
    for f in (video, rip):
        assert _first_audio_pts(f) >= 0.0, f.name
    assert _rip_offset(video, rip, 0) == 0.0


def _flash_times(path: Path) -> List[float]:
    out = ff.run_quiet([ff.ffmpeg_bin(), "-v", "error", "-copyts", "-i", str(path), "-map", "0:v:0", "-vf",
                        "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-", "-f", "null", "-"])
    times, cur = [], None
    for line in out.stdout.splitlines():
        if line.startswith("frame:"):
            cur = float(line.split("pts_time:")[1])
        elif "YAVG" in line and float(line.split("=")[1]) > 200 and (not times or cur - times[-1] > 0.5):
            times.append(cur)
    return times


def _beep_times(path: Path, track: int) -> List[float]:
    import numpy as np
    raw = ff.run_bytes([ff.ffmpeg_bin(), "-v", "error", "-i", str(path), "-map", f"0:a:{track}",
                              "-f", "f32le", "-ac", "1", "-ar", "48000", "-"]).stdout
    start = float(ff.run_quiet([ff.ffprobe_bin(), "-v", "error", "-select_streams", f"a:{track}",
                                "-show_entries", "stream=start_time", "-of", "csv=p=0",
                                str(path)]).stdout.split()[0].strip(","))
    loud = np.where(np.abs(np.frombuffer(raw, np.float32)) > 0.3)[0]
    times, last = [], -10 ** 9
    for k in loud:
        if k - last > 24000:
            times.append(k / 48000 + start)
        last = k
    return times


def _sync_errors(path: Path) -> List[float]:
    """Seconds each beep lands after its flash, for every beep on every track
    (beeps cut in half at the very start are left out)."""
    flashes, errs = _flash_times(path), []
    assert flashes, path.name
    for t in range(len(ff.probe(path, use_cache=False).audio)):
        for b in _beep_times(path, t):
            near = min(flashes, key=lambda f: abs(f - b))
            if abs(near - b) < 0.5 and near > flashes[0] + 0.1:     # not one cut in half at the start
                errs.append(b - near)
    return errs


@pytest.mark.parametrize("ext", [".mp4", ".mkv"])
@pytest.mark.parametrize("kind", ["copy", "smart", "encode"])
def test_parts_stay_in_sync_at_every_join(work, ext, kind):
    """Three parts, kept by copy, smart cut or re-encode, from MP4 and MKV:
    the sound stays within half an audio packet of the picture in every part
    (it used to drift further at each join, and MKV copies failed)."""
    src = fx.sync_clip(work / "sync", ext)
    out = fresh(work, f"out_sync_{kind}{ext}")
    c, logs = ctx()
    job = ClipJob(str(src), segments=[(2.5, 6.2), (8.3, 12.9), (15.7, 21.1)],
                  options=opts(out, exact_cuts=kind == "smart"), compress=kind == "encode",
                  encode=EncodeSettings(codec="h264", quality=18, preset="ultrafast").to_dict())
    r = run_job(job, c)
    assert r.status == "done", logs
    assert not any("instead" in line for line in logs), logs      # smart cut didn't fall back
    f = next(out.glob(f"*{ext}"))
    errs = _sync_errors(f)
    assert len(errs) >= 12, errs
    assert max(abs(e) for e in errs) < 0.012, errs


def test_lossless_fallback_merge_stays_in_sync(work):
    """Replays encoded separately, joined without re-encoding: each part runs
    to the end of its file, and the sound must still not drift at the joins
    (a file's last audio packet is marked short, but is played in full)."""
    clips = fx.sync_replays(work / "sync_replays")
    out = fresh(work, "out_sync_merge")
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips], options=opts(out, merge_method="lossless")), c)
    assert r.status == "done", logs
    errs = _sync_errors(next(out.glob("Merged*.mp4")))
    assert len(errs) >= 40, errs
    assert max(abs(e) for e in errs) < 0.012, errs


def test_volume_and_mix_without_compressing(work):
    src = fx.sync_clip(work / "sync", ".mp4")
    for name, kw in (("vol", dict(volumes={"1": 0.5})), ("mix", dict(mix_tracks=[0, 1]))):
        out = fresh(work, f"out_copy_{name}")
        c, logs = ctx()
        r = run_job(ClipJob(str(src), segments=[(9.3, None)], options=opts(out), **kw), c)
        assert r.status == "done", logs
        errs = _sync_errors(next(out.glob("*.mp4")))
        assert errs and max(abs(e) for e in errs) < 0.012, (name, errs)


def test_mixed_track_is_not_delayed(work):
    src = fx.sync_clip(work / "sync", ".mp4")
    out = fresh(work, "out_mix_delay")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(9.3, None)], mix_tracks=[0, 1], compress=True,
                        encode=EncodeSettings(codec="h264", quality=18, preset="ultrafast").to_dict(),
                        options=opts(out)), c)
    assert r.status == "done", logs
    errs = _sync_errors(next(out.glob("*.mp4")))
    assert errs and max(abs(e) for e in errs) < 0.003, errs


def test_skipped_output_keeps_the_original(work):
    d = fresh(work, "skip_keep")
    src = d / "clip.mp4"
    shutil.copy(fx.single_clip(work / "single"), src)
    out = fresh(work, "out_skip_keep")
    (out / "clip.mp4").write_bytes(b"something else")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), rip_audio_tracks=[0],
                        options=opts(out, conflict_policy="skip", delete_originals=True, permanent_delete=True)), c)
    assert r.status == "done" and len(r.outputs) == 1, logs
    assert src.exists() and not r.deleted
    assert any("Original kept" in m for m in r.messages)


def test_merge_keeps_the_clips_of_a_skipped_session(work):
    d = fresh(work, "skip_session")
    paths = fx.shared_replays(d)
    out = fresh(work, "out_skip_session")
    first = run_job(MergeJob(clip_paths=[str(p) for p in paths[:4]], options=opts(out)), ctx()[0])
    Path(first.outputs[0]).write_bytes(b"not the merge")
    c, logs = ctx()
    r = run_job(MergeJob(clip_paths=[str(p) for p in paths],
                         options=opts(out, conflict_policy="skip", delete_originals=True, permanent_delete=True)), c)
    assert r.status == "done", logs
    assert [Path(p).name for p in r.deleted] == [paths[4].name]     # the session that really saved
    assert all(p.exists() for p in paths[:4])


def test_delete_waits_when_a_later_job_needs_the_original(work):
    d = fresh(work, "delete_wait")
    src = d / "clip.mp4"
    shutil.copy(fx.single_clip(work / "single"), src)
    out = fresh(work, "out_delete_wait")
    c, logs = ctx()
    c.still_needed = lambda p: True
    r = run_job(ClipJob(str(src), segments=[(0, 5)],
                        options=opts(out, delete_originals=True, permanent_delete=True)), c)
    assert r.status == "done" and src.exists() and r.deferred == [str(src)], logs


def test_auto3_target_and_trials_at_the_output_size(work):
    from trimmer.core import encode as en
    src = fx.single_clip(work / "single")
    info = ff.probe(src)
    s = EncodeSettings(codec="h264", quality="auto3", preset="ultrafast", max_height=240, auto3_target=95)
    assert en.EncodeSettings.from_dict({**s.to_dict(), "auto3_target": "junk"}).auto3_target == 93.0
    assert en.ssim_target(93) == 97.0 and en.ssim_target(95) == 98.0
    with tempfile.TemporaryDirectory() as td:
        ref, _ = en.build_reference(src, info.duration, Path(td), filters=en.video_filters(s, info))
        assert ff.probe(ref, use_cache=False).height == 240      # measured at the size it's saved at
    out = fresh(work, "out_auto3_target")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), compress=True, encode=s.to_dict(), options=opts(out)), c)
    assert r.status == "done", logs
    assert re.search(r"CRF\d+ auto3-95", Path(r.outputs[0]).name), r.outputs


@pytest.mark.parametrize("exact", [True, False])
def test_split_parts_lose_nothing(work, exact):
    """A clip split at 7.3 s and 13.1 s: the three files together hold every
    frame of the source (frame-exact: each exactly once, in order)."""
    src = fx.single_clip(work / "single")
    out = fresh(work, f"out_split_{exact}")
    c, logs = ctx()
    files = []
    for k, seg in enumerate([(0.0, 7.3), (7.3, 13.1), (13.1, None)]):
        r = run_job(ClipJob(str(src), segments=[seg], output_name=f"part {k}",
                            options=opts(out, exact_cuts=exact)), c)
        assert r.status == "done", logs
        files.append(r.outputs[0])
    full = _decoded(src)
    got = [_decoded(f) for f in files]
    if exact:
        # Every source frame once, in order: the frame count adds up and every
        # frame that wasn't re-encoded at a cut is the source's own, in place.
        joined = sum(got, [])
        assert len(joined) == len(full), [len(g) for g in got]
        assert sum(a == b for a, b in zip(joined, full)) >= len(full) - 3 * 60
    else:
        assert set(full) <= set(sum(got, []))      # copies start on a keyframe, so they may overlap


def test_auto3_size_prediction_reuses_its_trials(work, monkeypatch):
    from trimmer.core import encode as en
    src = fx.single_clip(work / "single")
    info = ff.probe(src)
    s = EncodeSettings(codec="h264", quality="auto3", preset="ultrafast")
    crf, _ = en.estimate_crf_measured(src, info, s)

    def no_encode(*a, **k):
        raise AssertionError("predict_size encoded again")
    monkeypatch.setattr(en, "_encode_kbps", no_encode)
    assert en.predict_size(src, info, s, crf, info.duration) > 0


# ------------------------------------------------- gaps already in the source

def test_gap_in_the_source_is_reported_not_failed(work):
    """A compressed trim of footage that has no frames for a while (the
    recording dropped them) is saved; the gap is reported, where it is in
    the output and in the source."""
    src = fx.gap_clip(work / "gap")
    out = fresh(work, "out_gap_clip")
    enc = EncodeSettings(codec="h264", quality=28, preset="ultrafast").to_dict()
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(2.0, 18.0)], compress=True, encode=enc, options=opts(out)), c)
    assert r.status == "done", logs
    assert len(r.warnings) == 1, r.warnings
    m = re.match(r"Gap already in the source: (\d+\.\d+)s with no frames at 0:(\d+\.\d+) "
                 r"\(Gap clip\.mkv at 0:(\d+\.\d+)\)", r.warnings[0])
    assert m, r.warnings[0]
    length, at, src_at = map(float, m.groups())
    assert abs(length - 2.53) < 0.1 and abs(at - 6.0) < 0.2 and abs(src_at - 8.0) < 0.1
    assert any(l.startswith("Gap already in the source") for l in logs)


def test_copy_trim_reports_the_source_gap_too(work):
    src = fx.gap_clip(work / "gap")
    out = fresh(work, "out_gap_copy")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(0.0, 15.0)], options=opts(out, exact_cuts=False)), c)
    assert r.status == "done", logs
    assert len(r.warnings) == 1 and "at 0:07.9" in r.warnings[0], r.warnings
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(12.0, 18.0)], options=opts(out, exact_cuts=False)), c)
    assert r.status == "done" and r.warnings == [], r.warnings        # the part kept has no gap


def test_a_gap_the_source_doesnt_have_still_fails(work):
    from trimmer.core.jobs import SourceGap, check_frames
    gapped = fx.gap_clip(work / "gap")
    plain = fx.single_clip(work / "single")
    info_g, info_p = ff.probe(gapped), ff.probe(plain)
    # The gapped file checked as if made from footage without a gap: a fault.
    problem = check_frames(gapped, 20.0, 30.0, [(plain, info_p, 0.0, 20.0)])
    assert problem and "isn't in the source" in problem
    # From a source with the gap somewhere else: still a fault.
    problem = check_frames(gapped, 20.0, 30.0, [(gapped, info_g, 4.0, 20.0)])
    assert problem and "gap in its frames at 0:07.9" in problem
    # From itself: fine, and the gap is handed back.
    found: List[SourceGap] = []
    assert check_frames(gapped, 20.0, 30.0, [(gapped, info_g, 0.0, 20.0)], found) is None
    assert len(found) == 1 and abs(found[0].length - 2.53) < 0.05
    # Without sources, as before.
    assert "gap in its frames" in check_frames(gapped, 20.0, 30.0)


def test_damaged_frames_in_the_source_are_reported_not_failed(work):
    """Frames that are in the file but can't be decoded leave a gap in a
    re-encode with no gap in the packet times: decoding the source there
    shows it was already so."""
    src = fx.damaged_clip(work / "damaged")
    assert not pk.read_pts(src) or max(b - a for a, b in zip(pk.read_pts(src), pk.read_pts(src)[1:])) < 0.1
    out = fresh(work, "out_damaged")
    enc = EncodeSettings(codec="h264", quality=28, preset="ultrafast").to_dict()
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(2.0, 18.0)], compress=True, encode=enc, options=opts(out)), c)
    assert r.status == "done", logs
    assert len(r.warnings) == 1 and "damaged frames that can't be played" in r.warnings[0], r.warnings
    at = float(re.search(r"played at 0:(\d+\.\d+)", r.warnings[0]).group(1))
    assert abs(at - 6.0) < 0.2, r.warnings


@pytest.mark.parametrize("compress,packets", [(True, True), (False, True), (True, False)])
def test_merge_of_replays_with_a_source_gap(work, compress, packets):
    """A merge (packet join or the fallback, compressed or not) of replays
    that share a gap is saved, and the info file lists the gap."""
    clips = fx.gap_replays(work / "gap_replays")
    out = fresh(work, f"out_gap_merge_{compress}_{packets}")
    enc = EncodeSettings(codec="h264", quality=28, preset="ultrafast").to_dict()
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips], compress=compress, encode=enc,
                         options=opts(out, generate_clip_log=True, use_packet_matching=packets)), c)
    assert r.status == "done", logs
    assert any("Exact overlap found" in l for l in logs) == packets, logs
    assert len(r.warnings) == 1, r.warnings
    at = float(re.search(r"2\.5\ds with no frames at 0:(\d+\.\d+)", r.warnings[0]).group(1))
    assert abs(at - 8.0) < 0.1, r.warnings
    info = next(out.glob("*info.txt")).read_text(encoding="utf-8")
    assert "Gaps already in the source" in info and r.warnings[0].split(": ", 1)[1] in info, info


@pytest.mark.parametrize("codec", ["aac", "flac"])
def test_copied_audio_rips_keep_track_names_and_length(work, codec):
    """Rips carry the track's own name (not just the file title), and a copied FLAC
    rip reports its own length, not the whole source's (FLAC copies kept STREAMINFO)."""
    src = work / f"named {codec}.mkv"
    if not src.exists():
        fx.run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=8", "-f", "lavfi", "-i", "sine=f=440:d=8",
               "-f", "lavfi", "-i", "sine=f=880:d=8", "-map", "0", "-map", "1", "-map", "2",
               "-c:v", "libx264", "-g", "30", "-c:a", codec,
               "-metadata:s:a:0", "title=Mic", "-metadata:s:a:1", "title=Desktop", str(src))
    out = fresh(work, f"out_named_rips_{codec}")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(1.0, 4.0)], rip_audio_tracks=[0, 1],
                        options=opts(out, rip_format="copy")), c)
    assert r.status == "done", logs
    video = next(out.glob("*.mkv"))
    length = ff.probe(video, use_cache=False).duration
    for track, name in ((1, "Mic"), (2, "Desktop")):
        rip = next(out.glob(f"* - Track {track} ({name}).*"))
        info = ff.probe(rip, use_cache=False)
        assert abs(info.duration - length) < 0.3, (rip.name, info.duration, length)   # not the 8 s source
        if codec == "aac":
            assert [a.title for a in info.audio] == [name]
