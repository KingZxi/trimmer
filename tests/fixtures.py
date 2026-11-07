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
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from trimmer.core.jobs import JobContext, JobOptions

BASE = dt.datetime(2026, 10, 20, 20, 0, 0)
# The long source encodes take seconds each, so they are made once here and copied wherever a test
# asks for them. Each name carries a hash of its ffmpeg arguments: changing them makes a new file.
STOCK = Path(os.environ.get("TRIMMER_TEST_DIR", tempfile.gettempdir())) / "trimmer_stock"


def fresh(work: Path, name: str) -> Path:
    """An empty folder `name` inside `work`."""
    d = work / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


def ctx(logs=None):
    """A job context that collects the log lines: (context, lines)."""
    logs = logs if logs is not None else []
    return JobContext(log=logs.append), logs


def opts(out, **kw) -> JobOptions:
    """Job options that save into `out` and rename on a name clash."""
    o = JobOptions(output_dir=str(out), conflict_policy="rename")
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def run(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def replay_name(offset_s: int, ext=".mp4", extra="") -> str:
    t = BASE + dt.timedelta(seconds=offset_s)
    return f"Replay {t.strftime('%Y-%m-%d %H-%M-%S')}{extra}{ext}"


def _made(name: str, inputs: list, args: list) -> Path:
    """The stock file made by `ffmpeg <inputs> <args>` (made the first time it is asked for)."""
    key = hashlib.sha1(repr((inputs, args)).encode()).hexdigest()[:10]
    stock = STOCK / f"{Path(name).stem}-{key}{Path(name).suffix}"
    if not stock.exists():
        STOCK.mkdir(parents=True, exist_ok=True)
        making = stock.with_name("making-" + stock.name)
        run(*inputs, *args, str(making))
        making.replace(stock)
    return stock


def _copied(made: Path, out: Path) -> Path:
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(made, out)
    return out


def _master(seconds) -> Path:
    return _made("master.mkv",
                 ["-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d={seconds}",
                  "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:r=48000:a=0.3:seed=7",
                  "-f", "lavfi", "-i", f"aevalsrc=0.2*sin(2*PI*(200+40*t)*t):s=48000:d={seconds}"],
                 ["-map", "0:v", "-map", "1:a", "-map", "2:a",
                  "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", "-c:a", "flac"])


def master(folder: Path, seconds=70) -> Path:
    """folder/master.mkv: a moving picture + two distinctive audio tracks (Mic, Desktop)."""
    return _copied(_master(seconds), folder / "master.mkv")


def long_encode(folder: Path, seconds=70) -> Path:
    """folder/obs_stream.mp4, the 'OBS encoder output': one continuous H.264/AAC stream, 2 s GOP."""
    out = folder / "obs_stream.mp4"
    if out.exists():
        return out
    made = _made("obs_stream.mp4", ["-i", str(_master(seconds))],
                 ["-map", "0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                  "-g", "60", "-bf", "2", "-c:a", "aac", "-b:a", "128k",
                  "-metadata:s:a:0", "handler_name=Mic", "-metadata:s:a:1", "handler_name=Desktop"])
    return _copied(made, out)


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


def sync_replays(folder: Path):
    """Replays at 0-20, 12-32 and 28-48 of one session, each encoded on its
    own (so only the sound can line them up), with a flash and a beep at
    every odd second over noise. Their audio runs on a little past the
    picture, as OBS's does."""
    folder.mkdir(parents=True, exist_ok=True)
    m = folder / "sync master.mkv"
    if not m.exists():
        flash = "lt(mod(t-1,2),0.0167)*gte(t,1)"
        beep = "if(gte(t\\,1)*lt(mod(t-1\\,2)\\,0.05),0.8*sin(2*PI*1000*t),0)"
        run("-f", "lavfi", "-i", f"testsrc2=s=320x180:r=60:d=50,drawbox=c=white:t=fill:enable='{flash}'",
            "-f", "lavfi", "-i", f"aevalsrc='{beep}':s=48000:d=50",
            "-f", "lavfi", "-i", "anoisesrc=d=50:c=pink:r=48000:a=0.1:seed=3",
            "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0[a]", "-map", "0:v", "-map", "[a]",
            "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", "-c:a", "flac", str(m))
    paths = []
    for s, e in ((0, 20), (12, 32), (28, 48)):
        p = folder / replay_name(s)
        if not p.exists():
            run("-ss", str(s), "-i", str(m), "-t", str(e - s), "-map", "0:v", "-map", "0:a", "-map", "0:a",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-g", "120", "-bf", "2",
                "-x264-params", "scenecut=0", "-c:a", "aac", "-b:a", "128k", "-af", "apad=pad_dur=0.04",
                "-metadata:s:a:0", "handler_name=Mic", "-metadata:s:a:1", "handler_name=Desktop", str(p))
        paths.append(p)
    return paths


def gap_clip(folder: Path, seconds=20, gap=(8.0, 10.5), name="Gap clip.mkv") -> Path:
    """A clip whose picture stops for a while (no frames from gap[0] to
    gap[1]) while the sound runs on, like a recording that dropped frames
    when the game stalled. MKV keeps the gap through a re-encode (an MP4
    output is evened out to a constant frame rate)."""
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    if p.exists():
        return p
    a, b = gap
    run("-f", "lavfi", "-i", f"testsrc2=s=320x180:r=30:d={seconds},select='not(between(t,{a},{b - 0.001}))'",
        "-f", "lavfi", "-i", f"sine=f=440:r=48000:d={seconds}",
        "-map", "0:v", "-map", "1:a", "-fps_mode", "passthrough",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", "60", "-bf", "2",
        "-c:a", "aac", "-b:a", "128k", str(p))
    return p


def gap_replays(folder: Path):
    """Two replays of one session cut by stream copy from a gap clip
    (0-12 and 6-20), so they share packets and the gap (8-10.5 s) is in
    both."""
    folder.mkdir(parents=True, exist_ok=True)
    src = gap_clip(folder.parent, name="Gap stream.mkv")
    paths = []
    for s, e in ((0, 12), (6, 20)):
        p = folder / replay_name(s, ".mkv")
        if not p.exists():
            run("-ss", str(s), "-i", str(src), "-t", str(e - s), "-map", "0", "-c", "copy",
                "-avoid_negative_ts", "make_zero", str(p))
        paths.append(p)
    return paths


def damaged_clip(folder: Path, seconds=20, bad=(8.0, 10.5)) -> Path:
    """A clip with every packet present but the ones from bad[0] to bad[1]
    scrambled, so the decoder drops those frames: players and re-encodes
    show a gap that the packet times don't."""
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / "Damaged clip.mkv"
    if p.exists():
        return p
    good = folder / "undamaged.mkv"
    run("-f", "lavfi", "-i", f"testsrc2=s=320x180:r=30:d={seconds}",
        "-f", "lavfi", "-i", f"sine=f=440:r=48000:d={seconds}", "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-g", "60", "-bf", "2", "-x264-params", "scenecut=0",
        "-c:a", "aac", "-b:a", "128k", str(good))
    run("-v", "quiet", "-i", str(good), "-map", "0", "-c", "copy",
        "-bsf:v", f"noise=amount='if(between(pts*tb,{bad[0]},{bad[1]}),2,0)'", str(p))
    good.unlink()
    return p


def cut_off_clip(folder: Path) -> Path:
    """A recording cut off by a crash: its header still claims all 20 s, but the last quarter
    of the file is missing, so only about the first 15 s can be read."""
    p = folder / "Broken clip.mkv"
    if p.exists():
        return p
    full = folder / "full.mkv"
    run("-i", str(single_clip(folder.parent / "single")), "-c", "copy", str(full))
    data = full.read_bytes()
    p.write_bytes(data[: len(data) * 3 // 4])
    full.unlink()
    return p
