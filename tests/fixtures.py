"""Synthetic test clips.

Two kinds of fake OBS replays:
- shared:      cut by stream copy from ONE long encode, so overlapping
               clips share identical packets (what real OBS replay saves
               are expected to do).
- independent: each clip encoded separately from the same source, so
               nothing matches byte for byte and the audio/timestamp
               fallback has to do the work.
"""

import datetime as dt
import subprocess
from pathlib import Path

BASE = dt.datetime(2026, 10, 20, 20, 0, 0)


def run(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def replay_name(offset_s: int, ext=".mp4", extra="") -> str:
    t = BASE + dt.timedelta(seconds=offset_s)
    return f"Replay {t.strftime('%Y-%m-%d %H-%M-%S')}{extra}{ext}"


def master(folder: Path, seconds=70) -> Path:
    """Moving picture + two distinctive audio tracks (Mic, Desktop)."""
    out = folder / "master.mkv"
    if out.exists():
        return out
    run("-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d={seconds}",
        "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:r=48000:a=0.3:seed=7",
        "-f", "lavfi", "-i", f"aevalsrc=0.2*sin(2*PI*(200+40*t)*t):s=48000:d={seconds}",
        "-map", "0:v", "-map", "1:a", "-map", "2:a",
        "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", "-c:a", "flac", str(out))
    return out


def long_encode(folder: Path, seconds=70) -> Path:
    """The 'OBS encoder output': one continuous H.264/AAC stream, 2 s GOP."""
    out = folder / "obs_stream.mp4"
    if out.exists():
        return out
    m = master(folder, seconds)
    run("-i", str(m), "-map", "0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-g", "60", "-bf", "2", "-c:a", "aac", "-b:a", "128k",
        "-metadata:s:a:0", "handler_name=Mic", "-metadata:s:a:1", "handler_name=Desktop", str(out))
    return out


def shared_replays(folder: Path):
    """Clips at 0-20, 12-32, 14-30 (inside the second), 28-48, then 56-66
    after a gap. Returns the session-1 paths and the separate clip."""
    folder.mkdir(parents=True, exist_ok=True)
    src = long_encode(folder.parent)
    spans = [(0, 20), (12, 32), (14, 30), (28, 48), (56, 66)]
    paths = []
    for s, e in spans:
        p = folder / replay_name(s)
        run("-ss", str(s), "-i", str(src), "-t", str(e - s), "-map", "0", "-c", "copy",
            "-avoid_negative_ts", "make_zero", str(p))
        paths.append(p)
    return paths


def independent_replays(folder: Path, wrong_offset=1):
    """Same layout as shared_replays but each clip encoded on its own. The
    second clip's filename is `wrong_offset` seconds off, so only audio
    alignment can find the true overlap."""
    folder.mkdir(parents=True, exist_ok=True)
    m = master(folder.parent)
    spans = [(0, 20), (12, 32), (28, 48)]
    paths = []
    for i, (s, e) in enumerate(spans):
        name_t = s + (wrong_offset if i == 1 else 0)
        p = folder / replay_name(name_t)
        run("-ss", str(s), "-i", str(m), "-t", str(e - s), "-map", "0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", "60",
            "-c:a", "aac", "-b:a", "128k",
            "-metadata:s:a:0", "handler_name=Mic", "-metadata:s:a:1", "handler_name=Desktop", str(p))
        paths.append(p)
    return paths


def single_clip(folder: Path, seconds=20) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / "Gameplay clip.mp4"
    src = long_encode(folder.parent)
    run("-ss", "10", "-i", str(src), "-t", str(seconds), "-map", "0", "-c", "copy",
        "-avoid_negative_ts", "make_zero", str(p))
    return p


def sync_clip(folder: Path, ext=".mp4", seconds=24) -> Path:
    """A white frame and a beep on both audio tracks at every odd second, so
    sound and picture can be lined up in any output. H.264 with B-frames and
    a 2 s keyframe interval, like an OBS replay."""
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"Sync clip{ext}"
    if p.exists():
        return p
    flash = "gte(t,1)*lt(mod(t-1,2),0.0167)"
    beep = "if(gte(t\\,1)*lt(mod(t-1\\,2)\\,0.05),0.8*sin(2*PI*{f}*t),0.05*sin(2*PI*{b}*t))"
    run("-f", "lavfi", "-i", f"testsrc2=s=320x180:r=60:d={seconds},"
                             f"drawbox=c=white:t=fill:enable='{flash}'",
        "-f", "lavfi", "-i", f"aevalsrc='{beep.format(f=1000, b=300)}':s=48000:d={seconds}",
        "-f", "lavfi", "-i", f"aevalsrc='{beep.format(f=500, b=200)}':s=48000:d={seconds}",
        "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-g", "120", "-bf", "2", "-x264-params", "scenecut=0", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-metadata:s:a:0", "handler_name=Mic", "-metadata:s:a:1", "handler_name=Desktop", str(p))
    return p
