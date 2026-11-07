"""'Auto settings, measured': codec and CRF chosen per clip."""

import json
import subprocess
import time

from tests import fixtures as fx
from tests.fixtures import ctx, fresh, opts
from trimmer.core import encode as en
from trimmer.core import ffmpeg as ff
from trimmer.core import jobs as J
from trimmer.core.encode import EncodeSettings, give_up_target, pick_settings
from trimmer.core.jobs import ClipJob, MergeJob, run_job


def codec_of(path):
    out = subprocess.run([ff.ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=codec_name,pix_fmt", "-of", "json", str(path)],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return json.loads(out.stdout)["streams"][0]


def autoset(**kw):
    return EncodeSettings(**{"quality": "autoset", "preset": "ultrafast", **kw})


def test_give_up_target():
    assert give_up_target(0) == 99.0 and give_up_target(32) == 93.0 and abs(give_up_target(100) - 80.25) < 0.01
    s = EncodeSettings.from_dict({"quality": "autoset", "give_up": 500})
    assert s.give_up == 100 and s.is_autoset and s.needs_analysis
    assert not EncodeSettings.from_dict({"quality": "autoset", "chosen": {"codec": "x"}}).chosen
    s = EncodeSettings.from_dict({"quality": "autoset", "chosen": {"codec": "av1", "quality": 30, "ten_bit": True}})
    a = s.applied()
    assert (a.codec, a.quality, a.ten_bit, a.method, a.chosen) == ("av1", 30, True, "autoset", {})
    assert en.quality_label(a, 30, "autoset") == " (LOSSY), (CRF30 autoset, AV1 10-bit, medium)"


def test_pick_compares_codecs_and_is_remembered(work):
    src = fx.single_clip(work / "single")
    info = ff.probe(src)
    s = autoset()
    steps = []
    c = pick_settings(src, info, s, on_step=lambda d, n: steps.append((d, n)))
    assert c["codec"] in ("h264", "h265", "av1") and isinstance(c["quality"], int)
    assert steps[-1] == (15, 15)
    assert c["report"][0].startswith("Auto settings: ")
    assert sum(1 for line in c["report"] if " CRF " in line and "video" in line) == 3   # all three compared
    if c["met"]:
        assert c["score"] >= c["target"]
    t = time.time()
    assert pick_settings(src, info, s) == c and time.time() - t < 0.5            # remembered
    # Giving up more quality never picks a bigger file.
    more = pick_settings(src, info, autoset(give_up=90))
    assert more["kbps"] <= c["kbps"] * 1.02


def test_codec_that_cannot_be_used_is_skipped(work, monkeypatch):
    src = fx.single_clip(work / "single")
    real = ff.has_encoder
    monkeypatch.setattr(ff, "has_encoder", lambda name: False if name == "libsvtav1" else real(name))
    c = pick_settings(src, ff.probe(src), autoset(give_up=60))
    assert c["codec"] != "av1"
    assert any(line.strip().startswith("AV1 skipped") for line in c["report"])


def test_clip_job_analyses_and_uses_the_choice(work):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_autoset")
    c, logs = ctx()
    r = run_job(ClipJob(str(src), compress=True, encode=autoset(give_up=40).to_dict(), options=opts(out)), c)
    assert r.status == "done", logs
    f = next(out.glob("*.mp4"))
    chosen = pick_settings(src, ff.probe(src), autoset(give_up=40))          # remembered: instant
    name = {"h264": "H264", "h265": "H265", "av1": "AV1"}[chosen["codec"]]
    assert f"(CRF{chosen['quality']} autoset, {name}, ultrafast)" in f.name, f.name
    assert codec_of(f)["codec_name"] == {"h264": "h264", "h265": "hevc", "av1": "av1"}[chosen["codec"]]
    assert any(line.startswith("Auto settings: ") for line in logs)


def test_job_with_chosen_settings_skips_the_analysis(work, monkeypatch):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_autoset_chosen")
    monkeypatch.setattr(J, "pick_settings", lambda *a, **k: (_ for _ in ()).throw(AssertionError("analysed")))
    enc = autoset(chosen={"codec": "h264", "quality": 31, "ten_bit": False, "report": ["Auto settings: H264 CRF 31"]})
    job = ClipJob(str(src), compress=True, encode=enc.to_dict(), options=opts(out))
    assert not J.needs_analysis(job)
    c, logs = ctx()
    r = run_job(job, c)
    assert r.status == "done", logs
    f = next(out.glob("*.mp4"))
    assert "(CRF31 autoset, H264, ultrafast)" in f.name and codec_of(f)["codec_name"] == "h264"
    plain = ClipJob(str(src), compress=True, encode=autoset().to_dict(), options=opts(out))
    assert J.needs_analysis(plain)
    assert J.job_from_dict(json.loads(json.dumps(J.job_to_dict(job)))).encode["chosen"]["quality"] == 31


def test_merge_job_with_auto_settings(work):
    clips = fx.shared_replays(work / "shared")
    out = fresh(work, "out_autoset_merge")
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips[:3]], compress=True, encode=autoset(give_up=60).to_dict(),
                         options=opts(out)), c)
    assert r.status == "done", logs
    f = next(out.glob("Merged*.mp4"))
    assert "autoset" in f.name
    assert abs(ff.probe(f, use_cache=False).duration - 32.17) < 0.3      # "20-00-00 to 20-00-32"
