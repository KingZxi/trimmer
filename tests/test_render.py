"""Building blocks of the ffmpeg commands."""

from tests import fixtures as fx
from trimmer.core import ffmpeg as ff
from trimmer.core.render import AudioPlan, audio_mix_graph, copy_audio_maps


def test_a_mix_that_keeps_the_tracks_splits_them_first():
    plan = AudioPlan(mix=[0, 1], volumes={1: 0.5}, keep_originals=True)
    parts, outs, tracks = audio_mix_graph(plan, [0, 1, 2], {t: f"[c{t}]" for t in range(3)})
    assert parts == ["[c1]volume=0.500[g1]", "[c0]asplit=2[m0][k0]", "[g1]asplit=2[m1][k1]",
                     "[m0][m1]amix=inputs=2:normalize=0:duration=longest,alimiter=limit=0.98:latency=1[mixed]"]
    assert outs == ["[mixed]", "[k0]", "[k1]", "[c2]"]
    assert tracks == [None, 0, 1, 2]


def test_a_mix_that_replaces_the_tracks_leaves_only_the_others():
    parts, outs, tracks = audio_mix_graph(AudioPlan(mix=[0, 1], keep_originals=False), [0, 1, 2],
                                          {t: f"[1:a:{t}]" for t in range(3)})
    assert parts == ["[1:a:0][1:a:1]amix=inputs=2:normalize=0:duration=longest,alimiter=limit=0.98:latency=1[mixed]"]
    assert outs == ["[mixed]", "[1:a:2]"] and tracks == [None, 2]


def test_copied_tracks_are_mapped_straight_from_the_input(work):
    info = ff.probe(fx.single_clip(work / "single"))
    parts, maps, tracks, codec = copy_audio_maps(info, AudioPlan(mix=[0, 1]), [0, 1], ".mp4", input_index=1)
    assert maps == ["-map", "[mixed]", "-map", "[k0]", "-map", "[k1]"] and tracks == [None, 0, 1]
    parts, maps, tracks, codec = copy_audio_maps(info, AudioPlan(volumes={1: 2.0}), [0, 1], ".mp4", input_index=1)
    assert parts == ["[1:a:1]volume=2.000[g1]"] and maps == ["-map", "1:a:0", "-map", "[g1]"]
