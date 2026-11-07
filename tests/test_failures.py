"""A failed ffmpeg run is explained in plain words on the first line."""

import shutil
import sys
from pathlib import Path

import pytest

from tests import fixtures as fx
from tests.fixtures import ctx, fresh, opts
from trimmer.core import ffmpeg as ff
from trimmer.core.encode import EncodeSettings
from trimmer.core.ffmpeg import Cancelled
from trimmer.core.jobs import ClipJob, run_job


@pytest.mark.parametrize("stderr, meaning", [
    ("[out#0/mp4 @ 0x1] Error writing trailer: No space left on device\nConversion failed!", "disk is full"),
    ("Error opening output file D:\\x.part.mp4.\nError opening output files: Permission denied", "wouldn't let"),
    ("[h264_nvenc @ 0x1] OpenEncodeSessionEx failed: unsupported device (2): (no details)", "NVIDIA encoder"),
    ("[hevc_nvenc @ 0x1] Driver does not support the required nvenc API version.", "NVIDIA encoder"),
    ("[mov,mp4 @ 0x1] moov atom not found\nD:\\Replay.mp4: Invalid data found when processing input", "damaged"),
    ("[vost#0:0 @ 0x1] Unknown encoder 'libsvtav1'", "doesn't have the encoder"),
])
def test_common_failures_are_explained(stderr, meaning):
    assert meaning in ff.explain_failure(stderr)


def test_other_failures_show_ffmpegs_last_error_line():
    text = "Input #0, mov...\n  Stream #0:0: Video: h264\n[aac @ 0x1] Too many bits per frame requested\nConversion failed!\n"
    assert ff.explain_failure(text) == "[aac @ 0x1] Too many bits per frame requested"
    assert ff.explain_failure("") == "ffmpeg stopped with an error."


def test_failure_puts_the_reason_first_and_ffmpegs_output_after():
    e = ff.failure("Saving failed", ff.RunResult("failed", "something\nNo space left on device\n"))
    first, rest = str(e).split("\n", 1)
    assert first == "Saving failed: the disk is full. Free some space where the output is saved and retry."
    assert "No space left on device" in rest


@pytest.mark.skipif(sys.platform == "win32", reason="read-only folders work differently on Windows")
def test_a_real_failed_save_says_why(work, monkeypatch):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_fail")
    c, logs = ctx()
    real = ff.run_ffmpeg

    def no_space(args, *a, **k):        # ffmpeg as it fails when the disk fills up mid-encode
        r = real(args, *a, **k)
        if not r.ok:
            return r
        Path(k["output_path"]).unlink()  # run_ffmpeg removes a failed run's partial output
        return ff.RunResult("failed", "[out#0/mp4 @ 0x1] Error writing trailer: No space left on device\n"
                                      "Conversion failed!")
    monkeypatch.setattr(ff, "run_ffmpeg", no_space)
    enc = EncodeSettings(codec="h264", quality=30, preset="ultrafast").to_dict()
    r = run_job(ClipJob(str(src), compress=True, encode=enc, options=opts(out)), c)
    assert r.status == "failed"
    assert r.messages[0].splitlines()[0].startswith("Saving failed: the disk is full")
    assert not list(out.iterdir())          # nothing half-written was left behind
    shutil.rmtree(out, ignore_errors=True)


def test_a_cancelled_copy_leaves_nothing_behind(work, monkeypatch):
    src = fx.single_clip(work / "single")
    out = fresh(work, "out_cancel_copy")
    c, logs = ctx()
    real = shutil.copyfile

    def copy_then_cancel(a, b, *k, **kw):      # Cancel pressed while the file was being copied
        real(a, b, *k, **kw)
        c.cancel.cancel()
    monkeypatch.setattr(shutil, "copyfile", copy_then_cancel)
    with pytest.raises(Cancelled):
        run_job(ClipJob(str(src), options=opts(out, add_operation_tags=False)), c)
    assert not list(out.iterdir()), list(out.iterdir())


def test_an_audio_rip_of_a_cut_off_source_keeps_what_could_be_read(work):
    broken = fx.cut_off_clip(fresh(work, "damaged_rip_src"))
    out = fresh(work, "out_damaged_rip")
    c, logs = ctx()
    r = run_job(ClipJob(str(broken), make_video=False, rip_audio_tracks=[0], options=opts(out, rip_format="copy")), c)
    assert r.status == "failed" and r.messages[0].startswith("Track 1 failed verification"), (r.messages, logs)
    assert "damaged" in r.messages[0]
    [kept] = out.iterdir()
    assert " - Track 1" in kept.name and "(partial, first " in kept.name, kept.name
    assert 10 < ff.probe(kept, use_cache=False).duration < 19
