"""Rescuing a damaged recording (Queue > right-click a failed job > Rescue what can be read): the
parts that can still be read are found, copied untouched and the job runs on them."""

from tests import fixtures as fx
from tests.fixtures import ctx, fresh, opts
from trimmer.core import ffmpeg as ff
from trimmer.core import rescue as R
from trimmer.core.jobs import ClipJob, run_job
from trimmer.core.jobs.clip_job import RESCUE_FOLDER


def test_finds_the_readable_parts(work):
    src = fx.unreadable_middle_clip(work / "rescue_src")
    info = ff.probe(src, use_cache=False)
    st = R.readable_stretches(src, info)
    assert len(st) == 2 and R.is_damaged(st, info.duration), st
    (a, b), (c, d) = st
    assert a < 0.1 and 5 < b < 9 and b < c < 12 and d > info.duration - 0.5
    pieces = R.kept_pieces(st, info.duration)
    assert pieces[0] == (0.0, round(b - R.CUT_MARGIN, 3)) or abs(pieces[0][1] - (b - R.CUT_MARGIN)) < 1e-6
    assert pieces[1][0] == c and pieces[1][1] is None
    lost = R.lost_parts(st, info.duration)
    assert len(lost) == 1 and lost[0][0] < c


def test_an_undamaged_or_cut_off_file():
    assert not R.is_damaged([(0.0, 59.98)], 60.0)
    assert R.is_damaged([(0.0, 30.0)], 60.0)                       # ends early: nothing after
    assert R.kept_pieces([(0.0, 30.0)], 60.0) == [(0.0, 29.5)]
    assert R.lost_parts([(0.0, 30.0)], 60.0) == [(29.5, 60.0)]


def test_kept_ranges_follow_into_the_rescued_file():
    pieces = [(0.0, 29.5), (32.0, None)]                           # 29.5-32 lost
    assert R.map_time(10, pieces, 60) == 10 and R.map_time(30, pieces, 60) is None
    assert R.map_time(40, pieces, 60) == 37.5
    assert R.map_segments([(10, 20)], pieces, 60) == [(10, 20)]
    assert R.map_segments([(40, None)], pieces, 60) == [(37.5, None)]
    assert R.map_segments([(30, 31)], pieces, 60) == []             # all lost
    assert R.map_segments([(10, 50)], pieces, 60) == [(10, 47.5)]   # across the join: one range
    assert R.map_segments([(5, 8), (20, 45)], pieces, 60) == [(5, 8), (20, 42.5)]


def test_a_failed_job_can_be_rescued(work):
    src = fx.unreadable_middle_clip(work / "rescue_src")
    info = ff.probe(src, use_cache=False)
    out = fresh(work, "out_rescue")
    c, logs = ctx()
    # As before: the job fails because the output came out short.
    whole = [(0.0, info.duration)]          # trimmed to all of it (an untouched copy just copies the file)
    r = run_job(ClipJob(str(src), segments=whole, options=opts(out)), c)
    assert r.status == "failed" and "s was expected" in r.messages[0], (r.messages, logs)
    # Rescued: everything readable, named so, the original untouched and never deleted.
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=whole, options=opts(out, rescue=True, delete_originals=True)), c)
    assert r.status == "done", (r.messages, logs)
    assert len(r.outputs) == 1 and "(rescued)" in r.outputs[0] and r.outputs[0].endswith(".mp4")
    got = ff.probe(r.outputs[0], use_cache=False).duration
    st = R.readable_stretches(src, info)
    lost = st[1][0] - st[0][1] + R.CUT_MARGIN
    assert abs(got - (info.duration - lost)) < 1.0, (got, info.duration, lost)
    assert r.messages[0].startswith("Rescued:") and "couldn't be read" in r.messages[0]
    assert r.messages[0] in r.warnings
    assert src.exists() and not r.deleted
    assert not (out / RESCUE_FOLDER).exists() and not list(out.glob("*.part*"))


def test_a_rescued_trim_keeps_the_same_moments(work):
    src = fx.unreadable_middle_clip(work / "rescue_src")
    out = fresh(work, "out_rescue_trim")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), segments=[(2.0, 16.0)], options=opts(out, rescue=True)), c)
    assert r.status == "done", (r.messages, logs)
    got = ff.probe(r.outputs[0], use_cache=False).duration
    info = ff.probe(src, use_cache=False)
    st = R.readable_stretches(src, info)
    lost = st[1][0] - st[0][1] + R.CUT_MARGIN
    assert abs(got - (14.0 - lost)) < 1.2, (got, lost, logs)


def test_rescuing_an_undamaged_file_saves_it_as_usual(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_rescue_fine")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), options=opts(out, rescue=True)), c)
    assert r.status == "done" and "(rescued)" not in r.outputs[0]
    assert any("every part of the original could be read" in line for line in logs)


def test_a_rescued_compression(work):
    from trimmer.core.encode import EncodeSettings
    src = fx.unreadable_middle_clip(work / "rescue_src")
    out = fresh(work, "out_rescue_enc")
    c, logs = ctx()
    enc = EncodeSettings(codec="h264", quality=30, preset="ultrafast").to_dict()
    r = run_job(ClipJob(str(src), compress=True, encode=enc, options=opts(out, rescue=True)), c)
    assert r.status in ("done", "reverted"), (r.messages, logs)
    assert "(rescued)" in r.outputs[0] and ff.probe(r.outputs[0], use_cache=False).duration > 15
