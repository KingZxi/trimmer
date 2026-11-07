"""Size estimates: the same numbers in the Clips tab and the Queue, and the same rules as the job."""

from tests import fixtures as fx
from trimmer.core import ffmpeg as ff
from trimmer.core.encode import EncodeSettings
from trimmer.core.estimate import estimate_job, estimate_size, kept_seconds
from trimmer.core.jobs import ClipJob, MergeJob


def test_a_plain_copy_is_its_share_of_the_original(work):
    src = fx.single_clip(work / "single")
    info = ff.probe(src)
    assert estimate_size(src, info, info.duration / 2, None) == info.size // 2


def test_lossless_is_unknown_unless_the_original_is_kept_when_bigger(work):
    src = fx.single_clip(work / "single")
    info = ff.probe(src)
    lossless = EncodeSettings(mode="lossless", codec="h264")
    assert estimate_size(src, info, info.duration, lossless) is None
    kept = EncodeSettings(mode="lossless", codec="h264", revert_if_larger=True)
    assert estimate_size(src, info, info.duration, kept) == info.size


def test_never_more_than_the_original_when_it_is_kept_if_bigger(work):
    src = fx.single_clip(work / "single")
    info = ff.probe(src)
    big = EncodeSettings(mode="target_size", target_size_mb=500, codec="h264")
    assert estimate_size(src, info, info.duration, big) == 500 * 1024 * 1024
    assert estimate_size(src, info, info.duration, EncodeSettings(mode="target_size", target_size_mb=500,
                                                                   codec="h264", revert_if_larger=True)) == info.size
    small = estimate_size(src, info, info.duration, EncodeSettings(codec="h264", quality=40, preset="ultrafast"))
    assert 0 < small < info.size


def test_the_queue_estimates_a_trimmed_compressed_clip(work):
    src = fx.single_clip(work / "single")
    info = ff.probe(src)
    job = ClipJob(str(src), segments=[(2.0, 6.0), (10.0, None)], compress=True,
                  encode=EncodeSettings(codec="h264", quality=40, preset="ultrafast").to_dict())
    assert kept_seconds(job, info.duration) == 4.0 + info.duration - 10.0
    est, size = estimate_job(job)
    assert size == info.size and 0 < est < info.size
    assert estimate_job(MergeJob([str(src)]))[0] is None
