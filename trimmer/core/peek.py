"""A quick look inside a clip: audio peaks and a few thumbnails.

Both are for the trim timeline only (spot the kill, the shout, the scene
change without scrubbing), so they favour speed over fidelity. Results are
cached in memory per file, like the packet lists.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import ffmpeg as ff

RATE = 8000                     # waveform decode rate (Hz, mono)
_cache: Dict[tuple, object] = {}


def _key(path: Path, kind, *args):
    p = Path(path)
    st = p.stat()
    return (str(p.resolve()), st.st_size, st.st_mtime, kind) + args


def _audio_streams(path: Path) -> int:
    cmd = [ff.ffprobe_bin(), "-v", "error", "-select_streams", "a", "-show_entries",
           "stream=index", "-of", "csv=p=0", str(path)]
    return len(ff.run_quiet(cmd).stdout.split())


def waveform(path, buckets=1000, track=None) -> np.ndarray:
    """Peak level (0..1) of `buckets` equal slices of the clip's audio. All
    audio tracks are mixed unless `track` picks one. No audio gives an
    empty array."""
    path = Path(path)
    key = _key(path, "wave", buckets, track)
    if key in _cache:
        return _cache[key]
    n = 1 if track is not None else _audio_streams(path)
    out = np.zeros(0, dtype=np.float32)
    if n:
        cmd = [ff.ffmpeg_bin(), "-v", "error", "-nostdin", "-i", str(path), "-vn"]
        if track is not None:
            cmd += ["-map", f"0:a:{track}"]
        elif n > 1:
            ins = "".join(f"[0:a:{i}]" for i in range(n))
            cmd += ["-filter_complex", f"{ins}amix=inputs={n}:normalize=0[a]", "-map", "[a]"]
        else:
            cmd += ["-map", "0:a:0"]
        cmd += ["-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
        raw = ff.run_bytes(cmd).stdout
        pcm = np.abs(np.frombuffer(raw[:len(raw) // 2 * 2], dtype="<i2").astype(np.float32)) / 32768.0
        if len(pcm):
            edges = np.linspace(0, len(pcm), min(buckets, len(pcm)) + 1).astype(int)
            out = np.minimum(np.maximum.reduceat(pcm, edges[:-1]), 1.0)
    _cache[key] = out
    return out


def _thumb(path: Path, t: float, height: int) -> Optional[bytes]:
    """One frame near `t`. Decoding only keyframes is several times faster;
    if that finds nothing (no keyframe before `t`), decode normally."""
    for fast in (True, False):
        cmd = [ff.ffmpeg_bin(), "-v", "error", "-nostdin"]
        if fast:
            cmd += ["-skip_frame", "nokey"]
        cmd += ["-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-an", "-sn",
                "-vf", f"scale=-2:{height}", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "-"]
        data = ff.run_bytes(cmd).stdout
        if data:
            return data
    return None


def thumbnails(path, duration, count, height=48) -> List[Tuple[float, bytes]]:
    """`count` JPEG frames (time, bytes) from evenly spaced points in the clip.
    Each comes from its own fast seek, run a few at a time."""
    path = Path(path)
    if duration <= 0 or count <= 0:
        return []
    key = _key(path, "thumbs", round(duration, 2), count, height)
    if key in _cache:
        return _cache[key]
    times = [duration * (i + 0.5) / count for i in range(count)]
    with ThreadPoolExecutor(max_workers=4) as ex:
        frames = list(ex.map(lambda t: _thumb(path, t, height), times))
    out = [(t, b) for t, b in zip(times, frames) if b]
    _cache[key] = out
    return out
