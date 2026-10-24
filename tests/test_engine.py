"""Engine tests on synthetic clips. Run: python -m pytest tests -q"""

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

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
        sec = int(re.search(r"(\d\d)\.mp4", lines[0]).group(1))
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
    assert len(list(folder.glob("*.mp4"))) == 3
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
    f = next(out.glob("*(trimmed).mp4"))
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
    info = ff.probe(vid, use_cache=False)
    assert abs(info.duration - 10.0) < 0.15
    assert len(info.audio) == 3 and info.audio[0].title == "Mixed"
    wavs = sorted(out.glob("*.wav"))
    assert len(wavs) == 2 and "(Mic)" in wavs[0].name
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
    assert names == ["Big play - Track 1 (Mic).wav", "Big play.mp4"], names

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
    f = next(out.glob("*(trimmed).mp4"))
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
