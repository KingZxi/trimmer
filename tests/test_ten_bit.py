"""10-bit colour for H.265 / AV1 compression."""

import json
import re
import subprocess

from tests import fixtures as fx
from tests.fixtures import ctx, fresh, opts
from trimmer.core import ffmpeg as ff
from trimmer.core.encode import EncodeSettings, quality_label, video_codec_args
from trimmer.core.ffmpeg import MediaInfo
from trimmer.core.jobs import ClipJob, MergeJob, run_job


def stream(path):
    out = subprocess.run([ff.ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=codec_name,pix_fmt,profile:format_tags=comment", "-of", "json", str(path)],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    data = json.loads(out.stdout)
    s = data["streams"][0]
    s["comment"] = data.get("format", {}).get("tags", {}).get("comment", "")
    return s


def compress(src, out, **kw):
    enc = EncodeSettings(**{"preset": "ultrafast", **kw}).to_dict()
    c, logs = ctx()
    r = run_job(ClipJob(str(src), compress=True, encode=enc, options=opts(out)), c)
    assert r.status == "done", logs
    return next(out.glob("*.mp4"))


def test_h265_and_av1_ten_bit(work):
    src = fx.single_clip(work / "single")
    for codec, name in (("h265", "hevc"), ("av1", "av1")):
        f = compress(src, fresh(work, f"out_10bit_{codec}"), codec=codec, quality=30, ten_bit=True)
        s = stream(f)
        assert s["codec_name"] == name and s["pix_fmt"] == "yuv420p10le", s
        assert f"{codec.upper()} 10-bit, ultrafast)" in f.name, f.name
        assert "10-bit" in s["comment"], s["comment"]
    assert stream(f)["profile"] in ("Main", "Professional")   # AV1 Main covers 10-bit
    f = compress(src, fresh(work, "out_10bit_h265b"), codec="h265", quality=30, ten_bit=True)
    assert stream(f)["profile"] == "Main 10"


def test_ten_bit_ignored_for_h264_lossless_and_when_off(work):
    src = fx.single_clip(work / "single")
    f = compress(src, fresh(work, "out_10bit_h264"), codec="h264", quality=30, ten_bit=True)
    assert stream(f)["pix_fmt"] == "yuv420p" and "10-bit" not in f.name
    f = compress(src, fresh(work, "out_10bit_off"), codec="h265", quality=30)
    assert stream(f)["pix_fmt"] == "yuv420p" and "10-bit" not in f.name
    f = compress(src, fresh(work, "out_10bit_lossless"), codec="h265", mode="lossless", ten_bit=True)
    assert stream(f)["pix_fmt"] == "yuv420p" and "10-bit" not in f.name


def test_ten_bit_target_size_and_auto3(work):
    src = fx.single_clip(work / "single")
    f = compress(src, fresh(work, "out_10bit_target"), codec="h265", mode="target_size",
                 target_size_mb=1.0, audio="64", ten_bit=True)
    assert stream(f)["pix_fmt"] == "yuv420p10le"
    assert f.stat().st_size <= 1.1 * 1024 * 1024
    f = compress(src, fresh(work, "out_10bit_auto3"), codec="h265", quality="auto3", ten_bit=True)
    assert stream(f)["pix_fmt"] == "yuv420p10le" and re.search(r"CRF\d+ auto3, H265 10-bit", f.name), f.name


def test_ten_bit_compressed_merge(work):
    clips = fx.shared_replays(work / "shared")
    out = fresh(work, "out_10bit_merge")
    enc = EncodeSettings(codec="h265", quality=30, preset="ultrafast", ten_bit=True).to_dict()
    c, logs = ctx()
    r = run_job(MergeJob([str(p) for p in clips[:4]], compress=True, encode=enc, options=opts(out)), c)
    assert r.status == "done", logs
    f = next(out.glob("Merged*.mp4"))
    assert stream(f)["pix_fmt"] == "yuv420p10le"
    assert abs(ff.probe(f, use_cache=False).duration - 48.0) < 0.3


def test_ten_bit_settings_round_trip_and_hardware_args():
    s = EncodeSettings.from_dict({"codec": "h265", "ten_bit": "true"})
    assert s.ten_bit and EncodeSettings.from_dict(s.to_dict()).ten_bit
    assert not EncodeSettings.from_dict({}).ten_bit           # old settings files: off
    info = MediaInfo(path="x.mp4")
    info.pix_fmt = "yuv420p"
    for enc, codec, fmt, profile in (("nvenc", "h265", "p010le", "main10"), ("qsv", "h265", "p010le", "main10"),
                                     ("amf", "h265", "p010le", "main10"), ("nvenc", "av1", "p010le", None),
                                     ("software", "av1", "yuv420p10le", None)):
        a = video_codec_args(EncodeSettings(codec=codec, encoder=enc, ten_bit=True), info, quality=28)
        assert a[a.index("-pix_fmt") + 1] == fmt
        assert (a[a.index("-profile:v") + 1] if "-profile:v" in a else None) == profile
        assert a.count("-pix_fmt") == 1
    s = EncodeSettings(codec="h265", encoder="nvenc", ten_bit=True)
    assert quality_label(s, 28) == " (LOSSY), (CQ28, H265 10-bit NVENC, medium)"


def test_h265_target_size_two_pass(work):
    """x265's second pass used to stop on 'Incomplete CU-tree stats file'
    because pass 1 saw a different number of frames."""
    src = fx.single_clip(work / "single")
    f = compress(src, fresh(work, "out_h265_target"), codec="h265", mode="target_size",
                 target_size_mb=1.0, audio="64")
    assert stream(f)["pix_fmt"] == "yuv420p"
    assert f.stat().st_size <= 1.1 * 1024 * 1024
