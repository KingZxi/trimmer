"""The hidden "Trimmer" comment tag written into every saved file."""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TRIMMER_DATA_DIR", tempfile.mkdtemp(prefix="trimmer_data_"))

from trimmer.core import ffmpeg as ff                     # noqa: E402
from trimmer.core import tag as tg                         # noqa: E402
from trimmer.core.encode import EncodeSettings            # noqa: E402
from trimmer.core.jobs import ClipJob, JobContext, JobOptions, MergeJob, run_job  # noqa: E402
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


def ctx():
    logs = []
    return JobContext(log=logs.append), logs


def opts(out, **kw):
    o = JobOptions(output_dir=str(out), conflict_policy="rename")
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def tag_of(path):
    return ff.probe(path, use_cache=False).trimmer_tag


def test_trim_compress_and_video_only_carry_the_tag(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_tag_basic")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(2.0, 8.0)], options=opts(out)), c)
    assert r.status == "done", logs
    assert tag_of(r.outputs[0]) == "Trimmer: trimmed"
    enc = EncodeSettings(codec="h265", quality=30, preset="ultrafast").to_dict()
    r = run_job(ClipJob(str(src), compress=True, encode=enc, audio_tracks=[0], options=opts(out)), c)
    assert r.status == "done", logs
    assert tag_of(r.outputs[0]) == "Trimmer: compressed (H.265, q30), removed audio tracks"
    r = run_job(ClipJob(str(src), make_video=False, rip_video=True, options=opts(out)), c)
    assert tag_of(r.outputs[0]) == "Trimmer: video only"
    # A plain copy does nothing, so it stays untagged.
    r = run_job(ClipJob(str(src), options=opts(out)), c)
    assert tag_of(r.outputs[0]) == ""


def test_tag_is_cumulative(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_tag_chain")
    c, logs = ctx()
    r1 = run_job(ClipJob(str(src), segments=[(2.0, 12.0)], options=opts(out)), c)
    enc = EncodeSettings(codec="h265", quality=28, preset="ultrafast").to_dict()
    r2 = run_job(ClipJob(r1.outputs[0], compress=True, encode=enc, options=opts(out)), c)
    assert r2.status == "done", logs
    assert tag_of(r2.outputs[0]) == "Trimmer: trimmed; then compressed (H.265, q28)"
    r3 = run_job(ClipJob(r2.outputs[0], segments=[(0.0, 4.0)], options=opts(out)), c)
    assert tag_of(r3.outputs[0]) == "Trimmer: trimmed; then compressed (H.265, q28); then trimmed"
    # Ripping a tagged file keeps its history too.
    r4 = run_job(ClipJob(r2.outputs[0], make_video=False, rip_audio_tracks=[0],
                         options=opts(out, rip_format="flac")), c)
    assert tag_of(r4.outputs[0]) == "Trimmer: trimmed; then compressed (H.265, q28); then audio rip"
    assert not list(out.glob("*.part*"))


def test_tag_in_every_container_and_rip_format(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_tag_formats")
    c, logs = ctx()
    for container in (".mp4", ".mkv"):
        r = run_job(ClipJob(str(src), segments=[(1.0, 5.0)], options=opts(out, container=container)), c)
        assert r.status == "done", logs
        assert tag_of(r.outputs[0]) == "Trimmer: trimmed", container
    mov = fresh(work, "tag_mov") / "clip.mov"
    fx.run("-i", str(src), "-c", "copy", str(mov))
    r = run_job(ClipJob(str(mov), segments=[(1.0, 5.0)], options=opts(out)), c)
    assert Path(r.outputs[0]).suffix == ".mov" and tag_of(r.outputs[0]) == "Trimmer: trimmed"
    for fmt, ext in (("wav", ".wav"), ("flac", ".flac"), ("mp3", ".mp3"), ("m4a", ".m4a"), ("copy", ".m4a")):
        r = run_job(ClipJob(str(src), make_video=False, rip_audio_tracks=[0],
                            options=opts(out, rip_format=fmt)), c)
        assert r.status == "done", logs
        f = Path(r.outputs[0])
        assert f.suffix == ext and tag_of(f) == "Trimmer: audio rip", (fmt, f.name)
    # A source whose audio can't be copied into .m4a lands in .mka.
    mka_src = fresh(work, "tag_mka_src") / "opus.mkv"
    fx.run("-i", str(src), "-map", "0:v", "-map", "0:a:0", "-c:v", "copy", "-c:a", "libopus", str(mka_src))
    r = run_job(ClipJob(str(mka_src), make_video=False, rip_audio_tracks=[0],
                        options=opts(out, rip_format="copy")), c)
    assert r.status == "done", logs
    assert tag_of(r.outputs[0]) == "Trimmer: audio rip", r.outputs[0]


def test_smart_cut_output_carries_the_tag(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_tag_smart")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(5.3, 12.7)], options=opts(out, exact_cuts=True)), c)
    assert any(l.startswith("Smart cut: re-encoding") for l in logs), logs
    assert tag_of(r.outputs[0]) == "Trimmer: trimmed"


def test_merge_outputs_carry_the_tag(work):
    clips = [str(p) for p in fx.shared_replays(work / "shared")[:4]]
    c, logs = ctx()
    out = fresh(work, "out_tag_merge")
    r = run_job(MergeJob(clips, options=opts(out)), c)
    assert r.status == "done", logs
    assert tag_of(r.outputs[0]) == "Trimmer: merged (replay session)"
    enc = EncodeSettings(codec="h264", quality=28, preset="ultrafast").to_dict()
    out = fresh(work, "out_tag_merge_enc")
    r = run_job(MergeJob(clips, compress=True, encode=enc, options=opts(out)), c)
    assert tag_of(r.outputs[0]) == "Trimmer: merged (replay session), compressed (H.264, q28)"
    out = fresh(work, "out_tag_trim_mode")
    r = run_job(MergeJob(clips, options=opts(out, merge_mode="trim")), c)
    assert r.status == "done", logs
    assert r.outputs and all(tag_of(p) == "Trimmer: trimmed replay session" for p in r.outputs)
    # The timestamp/audio fallback tags too.
    ind = [str(p) for p in fx.independent_replays(work / "indep")[:3]]
    out = fresh(work, "out_tag_fallback")
    r = run_job(MergeJob(ind, options=opts(out)), c)
    assert r.status == "done", logs
    assert tag_of(r.outputs[0]).startswith("Trimmer: merged")
    assert not list(out.glob("*.part*"))


def test_tag_helpers_and_detection():
    assert tg.build_tag("", ["trimmed"]) == "Trimmer: trimmed"
    assert tg.build_tag("Trimmer: trimmed", []) == "Trimmer: trimmed"
    assert tg.tag_kinds("Trimmer: trimmed; then compressed (H.265, q28)") == {"trim", "compress"}
    assert tg.name_kinds("Clip (trimmed)") == {"trim"}
    assert tg.name_kinds("Clip (LOSSY), (CRF23, H265, medium)") == {"compress"}
    assert tg.name_kinds("Merged 2026-10-20 20-00-00 to 20-01-00") == {"merge"}
    assert tg.name_kinds("Plain") == set()


def test_trim_range_names():
    from trimmer.core.util import format_name_time, trim_range_label
    assert format_name_time(5.3) == "0m05s" and format_name_time(12.7) == "0m13s"
    assert format_name_time(65) == "1m05s" and format_name_time(3725) == "1h02m05s"
    assert trim_range_label([(65, 92)]) == "trimmed 1m05s-1m32s"
    assert trim_range_label([(12, 45), (70, 90), (100, 110), (120, 130)]) == \
        "trimmed 0m12s-0m45s, 1m10s-1m30s +2 more"
    label = f"Clip ({trim_range_label([(65, 92)])})"
    assert tg.name_kinds(label) == {"trim"}
    assert not any(ch in label for ch in '<>:"/\\|?*')
