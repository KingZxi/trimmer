""""Keep the original if compressing would make it bigger", on every way a file is saved.

Written before the save paths in jobs.py were brought together, to pin down what each one did:
the output's name, the job's status, what the log says, and that nothing half-written is left.
"""

from tests import fixtures as fx
from tests.fixtures import ctx, fresh, opts
from trimmer.core import ffmpeg as ff
from trimmer.core.encode import EncodeSettings, quality_label
from trimmer.core.jobs import NOT_COMPRESSED, ClipJob, JobContext, MergeJob, run_job

# Lossless is far bigger than these H.264 sources, so the encode is stopped part way.
BIGGER = EncodeSettings(mode="lossless", codec="h264", preset="ultrafast", revert_if_larger=True).to_dict()
# CRF 1 is too, and its sample encode already shows it, so the encode never starts.
PREDICTED = EncodeSettings(codec="h264", quality=1, preset="ultrafast", revert_if_larger=True).to_dict()
# CRF 40 really is smaller: it is kept compressed.
SMALLER = EncodeSettings(codec="h264", quality=40, preset="medium", revert_if_larger=True).to_dict()


def no_parts(folder):
    assert not list(folder.rglob("*.part*")), list(folder.rglob("*.part*"))


def length(path):
    return ff.probe(path, use_cache=False).duration


def test_clip_whose_encode_grows_too_big_keeps_the_original_footage(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "rv_clip_bigger")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), compress=True, encode=BIGGER, options=opts(out)), c)
    assert r.status == "reverted", logs
    [f] = out.glob("*.mp4")
    assert f.name == f"Gameplay clip{NOT_COMPRESSED}.mp4"
    assert any("larger than the original" in line and "keeping the original footage" in line for line in logs)
    assert abs(length(f) - length(src)) < 0.1
    assert ff.probe(f, use_cache=False).trimmer_tag == ""       # an untouched copy: nothing was done to it
    no_parts(out)


def test_clip_predicted_too_big_is_never_encoded(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "rv_clip_predicted")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), compress=True, encode=PREDICTED, options=opts(out)), c)
    assert r.status == "reverted", logs
    assert [f.name for f in out.glob("*.mp4")] == [f"Gameplay clip{NOT_COMPRESSED}.mp4"]
    assert any(line.startswith("Predicted size") and "skipping the encode" in line for line in logs), logs
    assert not any(line.startswith("Encoding") for line in logs)
    no_parts(out)


def test_no_name_clash_question_for_a_compressed_file_that_is_never_written(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "rv_clip_predicted_clash")
    taken = out / f"Gameplay clip{quality_label(EncodeSettings.from_dict(PREDICTED), 1)}.mp4"
    taken.write_bytes(b"someone else's file")
    asked, logs = [], []
    c = JobContext(log=logs.append, resolve_conflict=lambda p: asked.append(p.name) or "rename")
    r = run_job(ClipJob(str(src), compress=True, encode=PREDICTED, options=opts(out, conflict_policy="ask")), c)
    assert r.status == "reverted", logs
    assert asked == [], asked
    assert taken.read_bytes() == b"someone else's file"
    assert (out / f"Gameplay clip{NOT_COMPRESSED}.mp4").exists()


def test_trimmed_clip_falls_back_to_a_keyframe_copy_of_the_kept_part(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "rv_clip_trim")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(3.3, 12.0)], compress=True, encode=BIGGER, options=opts(out)), c)
    assert r.status == "reverted", logs
    [f] = out.glob("*.mp4")
    # A stream copy starts on the keyframe at or before the mark (2 s GOP: 2.0 s), and the name says so.
    assert f.name == f"Gameplay clip (trimmed 0m02s-0m12s){NOT_COMPRESSED}.mp4", f.name
    assert 9.8 <= length(f) <= 10.2
    assert ff.probe(f, use_cache=False).trimmer_tag == "Trimmer: trimmed"
    no_parts(out)


def test_clip_that_really_is_smaller_stays_compressed(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "rv_clip_smaller")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), compress=True, encode=SMALLER, options=opts(out)), c)
    assert r.status == "done", logs
    [f] = out.glob("*.mp4")
    assert "(CRF40, H264, medium)" in f.name and NOT_COMPRESSED not in f.name
    no_parts(out)


def test_video_only_rip_of_a_compressed_clip_reverts_too(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "rv_clip_rip_video")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), make_video=False, rip_video=True, compress=True, encode=BIGGER,
                        options=opts(out)), c)
    assert r.status == "reverted", logs
    [f] = out.glob("*.mp4")
    assert f.name == f"Gameplay clip - video only{NOT_COMPRESSED}.mp4", f.name
    assert not ff.probe(f, use_cache=False).audio
    no_parts(out)


def test_packet_trim_mode_saves_each_clip_uncompressed(work):
    clips = fx.shared_replays(work / "shared")
    out = fresh(work, "rv_packet_trim")
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips[:4]], compress=True, encode=BIGGER,
                         options=opts(out, merge_mode="trim")), c)
    assert r.status == "done", logs
    files = sorted(next(out.glob("Session *")).glob("*.mp4"))
    assert files and all(f.name.endswith(f"{NOT_COMPRESSED}.mp4") for f in files), files
    assert any("larger than a plain merge" in line for line in logs), logs
    no_parts(out)


def test_fallback_merge_predicted_too_big_is_merged_plainly(work):
    clips = fx.independent_replays(work / "indep", wrong_offset=1)
    out = fresh(work, "rv_fallback_predicted")
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips], compress=True, encode=PREDICTED,
                         options=opts(out, merge_method="lossless")), c)
    assert r.status == "done", logs
    [f] = out.glob("Merged*.mp4")
    assert f.name.endswith(f"{NOT_COMPRESSED}.mp4"), f.name
    assert any(line.startswith("Predicted size") and "merging without compressing" in line for line in logs)
    no_parts(out)


def test_fallback_trim_mode_that_really_is_smaller_stays_compressed(work):
    clips = fx.independent_replays(work / "indep", wrong_offset=1)
    out = fresh(work, "rv_fallback_trim_smaller")
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips], compress=True, encode=SMALLER,
                         options=opts(out, merge_mode="trim")), c)
    assert r.status == "done", logs
    files = sorted(next(out.glob("Session *")).glob("*.mp4"))
    assert len(files) == 3 and all("(CRF40, H264, medium)" in f.name for f in files), files
    no_parts(out)
