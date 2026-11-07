"""The one shared ffmpeg/ffprobe layer.

Replaces the two separate copies of probing and progress handling that the
old merge and compress scripts each had. Nothing here prints or prompts:
progress goes to a callback and cancellation comes from a CancelToken, so
it works the same from the GUI's worker thread and from the CLI.

Only local ffmpeg/ffprobe executables are ever run. No network access.
"""

import atexit
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from .util import safe_unlink

# On Windows, stop every ffmpeg call from flashing up a console window when
# the app runs without one (pythonw / packaged GUI).
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_ffmpeg_dir: Optional[Path] = None


def set_ffmpeg_dir(path) -> None:
    """Use ffmpeg/ffprobe from this folder instead of PATH (None = PATH)."""
    global _ffmpeg_dir
    _ffmpeg_dir = Path(path) if path else None
    _cache.clear()


def _beside_app() -> List[Path]:
    """An "ffmpeg" folder next to Trimmer (or its .exe), with or without a bin
    folder inside, so a downloaded ffmpeg can just be dropped in."""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    return [base / "ffmpeg" / "bin", base / "ffmpeg"]


def _tool(name: str) -> str:
    exe = name + (".exe" if sys.platform == "win32" else "")
    folders = ([_ffmpeg_dir] if _ffmpeg_dir is not None else []) + _beside_app()
    for folder in folders:
        candidate = folder / exe
        if candidate.exists():
            return str(candidate)
    return shutil.which(name) or name


def ffmpeg_bin() -> str:
    return _tool("ffmpeg")


def ffprobe_bin() -> str:
    return _tool("ffprobe")


def tools_available() -> List[str]:
    """Names of missing tools (empty list = all good)."""
    missing = []
    for name, path in (("ffmpeg", ffmpeg_bin()), ("ffprobe", ffprobe_bin())):
        if not (Path(path).exists() or shutil.which(path)):
            missing.append(name)
    return missing


# --------------------------------------------------------------------------
# Child processes: every ffmpeg/ffprobe Trimmer starts is tracked, so none
# can outlive the app. Closing Trimmer kills whatever is still running, and
# on Windows each one is also put in a "kill on close" job object, so even
# if Trimmer crashes or is ended from Task Manager, Windows ends them too.
# --------------------------------------------------------------------------

_children_lock = threading.Lock()
_children: set = set()
_closing = False            # set on quit: no new processes after that
_job = None                 # Windows job object handle (0 = unavailable)


def _windows_job():
    """A job object that kills every process in it when Trimmer's last handle
    to it closes, which Windows does when Trimmer exits for any reason."""
    global _job
    if _job is not None:
        return _job
    _job = 0
    try:
        import ctypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", ctypes.c_uint32),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", ctypes.c_uint32),
                        ("SchedulingClass", ctypes.c_uint32)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits),
                        ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.SetInformationJobObject.restype = ctypes.c_int
        k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k32.AssignProcessToJobObject.restype = ctypes.c_int
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return 0
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return 0                                            # 9 = JobObjectExtendedLimitInformation
        _job = (k32, job)       # never closed by us: Windows closes it when Trimmer exits
    except (AttributeError, OSError, ValueError):
        _job = 0
    return _job


def _spawn(cmd, **kwargs) -> subprocess.Popen:
    """Start a tracked child process (no console window on Windows)."""
    if _closing:
        raise Cancelled()
    process = subprocess.Popen(cmd, creationflags=_NO_WINDOW, **kwargs)
    if sys.platform == "win32":
        job = _windows_job()
        if job:
            try:
                job[0].AssignProcessToJobObject(job[1], int(process._handle))
            except (AttributeError, OSError, ValueError):
                pass
    with _children_lock:
        for p in [p for p in _children if p.returncode is not None]:
            _children.discard(p)
        _children.add(process)
    if _closing:                # quit started while it was starting
        _kill(process)
    return process


def _forget(process):
    with _children_lock:
        _children.discard(process)


def _kill(process):
    try:
        _suspend(process, False)            # a paused (frozen) process must be thawed first
        process.kill()
    except OSError:
        pass


def kill_all() -> int:
    """Kill every ffmpeg/ffprobe Trimmer started that is still running.
    Returns how many were still running."""
    with _children_lock:
        procs = [p for p in _children if p.returncode is None and p.poll() is None]
    for p in procs:
        _kill(p)
    return len(procs)


def shutdown():
    """On quit: kill everything still running and start nothing new."""
    global _closing
    _closing = True
    kill_all()


atexit.register(shutdown)


def _run(cmd, text: bool, cwd=None, timeout=None, cancel: "CancelToken" = None) -> subprocess.CompletedProcess:
    """subprocess.run, but tracked, and stopped at once when `cancel` is
    cancelled (raises Cancelled) instead of running on to the end."""
    empty = "" if text else b""
    try:
        process = _spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, cwd=cwd,
                         **(dict(text=True, encoding="utf-8", errors="replace") if text else {}))
    except Cancelled:
        if cancel is not None:
            raise
        return subprocess.CompletedProcess(cmd, 1, empty, "Trimmer is closing" if text else b"Trimmer is closing")
    if cancel is not None:
        cancel.register(process)            # so Pause can freeze it too
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            wait = 0.2 if cancel is not None else None
            if deadline is not None:
                left = max(0.0, deadline - time.monotonic())
                wait = left if wait is None else min(wait, left)
            try:
                out, err = process.communicate(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.cancelled:
                    _kill(process)
                    process.communicate()
                    raise Cancelled()
                if deadline is not None and time.monotonic() >= deadline:
                    _kill(process)
                    process.communicate()
                    raise subprocess.TimeoutExpired(cmd, timeout)
    finally:
        if cancel is not None:
            cancel.unregister(process)
        if process.poll() is None:          # e.g. an exception while waiting
            _kill(process)
            process.wait()
        _forget(process)
    return subprocess.CompletedProcess(cmd, process.returncode, out, err)


def run_quiet(cmd, cwd=None, timeout=None, cancel: "CancelToken" = None) -> subprocess.CompletedProcess:
    """subprocess.run with captured text output and no console window."""
    return _run(cmd, True, cwd=cwd, timeout=timeout, cancel=cancel)


def run_bytes(cmd, timeout=None, cancel: "CancelToken" = None) -> subprocess.CompletedProcess:
    """Like run_quiet but stdout/stderr stay raw bytes (PCM, images)."""
    return _run(cmd, False, timeout=timeout, cancel=cancel)


class FFmpegError(RuntimeError):
    pass


class Cancelled(Exception):
    """Raised inside the engine when the user cancels a job."""


def _suspend(process, pause: bool):
    """Freeze or unfreeze a running process in place (no work is lost)."""
    try:
        if process.poll() is not None:
            return
        if sys.platform == "win32":
            import ctypes
            fn = ctypes.windll.ntdll.NtSuspendProcess if pause else ctypes.windll.ntdll.NtResumeProcess
            fn(ctypes.c_void_p(int(process._handle)))
        else:
            import signal
            os.kill(process.pid, signal.SIGSTOP if pause else signal.SIGCONT)
    except (OSError, AttributeError, ValueError):
        pass


class CancelToken:
    """Thread-safe cancel and pause control shared between the GUI and the
    worker. Pausing freezes the running ffmpeg where it is and holds the
    engine at its next check(); resuming carries on exactly from there."""

    def __init__(self):
        self._event = threading.Event()
        self._running = threading.Event()
        self._running.set()
        self._lock = threading.Lock()
        self._procs = set()

    def cancel(self):
        with self._lock:
            procs = list(self._procs)
        for p in procs:              # a frozen process must be thawed before it can be stopped
            _suspend(p, False)
        self._event.set()
        self._running.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def pause(self):
        with self._lock:
            self._running.clear()
            for p in self._procs:
                _suspend(p, True)

    def resume(self):
        with self._lock:
            for p in self._procs:
                _suspend(p, False)
            self._running.set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set() and not self._event.is_set()

    def register(self, process):
        with self._lock:
            self._procs.add(process)
            if not self._running.is_set():
                _suspend(process, True)

    def unregister(self, process):
        with self._lock:
            self._procs.discard(process)

    def check(self):
        while not self._running.wait(0.25):      # paused: wait here until resumed or cancelled
            if self._event.is_set():
                break
        if self._event.is_set():
            raise Cancelled()


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

GENERIC_HANDLERS = {"soundhandler", "sound media handler", "core media audio",
                    "videohandler", "video media handler", "core media video"}


def _clean_track_title(tags: dict):
    """Prefer a real stream title; ignore generic muxer handler names."""
    title = tags.get("title")
    if title:
        return title
    handler = tags.get("handler_name")
    if handler and handler.strip().lower() not in GENERIC_HANDLERS:
        return handler
    return None


def _parse_rate(text) -> float:
    try:
        if text and "/" in text:
            n, d = text.split("/")
            return float(n) / float(d) if float(d) else 0.0
        return float(text or 0)
    except ValueError:
        return 0.0


@dataclass
class AudioTrack:
    index: int                  # ordinal among audio streams (for -map 0:a:N)
    codec: str = ""
    channels: int = 0
    sample_rate: int = 0
    bit_rate: int = 0
    title: Optional[str] = None
    language: Optional[str] = None

    @property
    def label(self) -> str:
        return self.title or f"Track {self.index + 1}"


@dataclass
class MediaInfo:
    path: Path
    duration: float = 0.0
    start_time: float = 0.0
    size: int = 0
    bit_rate: int = 0
    format_name: str = ""
    has_video: bool = False
    video_codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pix_fmt: str = ""
    audio: List[AudioTrack] = field(default_factory=list)
    trimmer_tag: str = ""                  # Trimmer's own "Trimmer: ..." comment tag, "" when absent

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.has_video else "audio only"

    def stream_signature(self):
        """What has to match for clips to be joined by stream copy."""
        return (self.video_codec, self.width, self.height, self.pix_fmt,
                tuple((a.codec, a.channels, a.sample_rate) for a in self.audio))


_cache = {}


def probe(path, use_cache: bool = True) -> MediaInfo:
    path = Path(path)
    try:
        st = path.stat()
        key = (str(path.resolve()), st.st_size, st.st_mtime)
    except OSError as e:
        raise FFmpegError(f"Can't read {path.name}: {e}")
    if use_cache and key in _cache:
        return _cache[key]

    cmd = [ffprobe_bin(), "-v", "error",
           "-show_entries",
           "format=duration,size,bit_rate,format_name,start_time:format_tags=comment:"
           "stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,"
           "pix_fmt,channels,sample_rate,bit_rate:stream_tags=title,language,handler_name,comment:"
           "stream_disposition=attached_pic",
           "-of", "json", str(path)]
    try:
        out = run_quiet(cmd)
    except FileNotFoundError:
        raise FFmpegError("ffprobe was not found. Install ffmpeg or set its folder in Settings.")
    if out.returncode != 0:
        raise FFmpegError(f"ffprobe couldn't read {path.name}: {out.stderr.strip()[-500:]}")
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        raise FFmpegError(f"ffprobe returned unreadable output for {path.name}")

    fmt = data.get("format") or {}
    info = MediaInfo(path=path)
    info.duration = float(fmt.get("duration") or 0.0)
    try:
        info.start_time = float(fmt.get("start_time") or 0.0)
    except ValueError:
        info.start_time = 0.0
    info.size = int(fmt.get("size") or st.st_size)
    info.bit_rate = int(fmt.get("bit_rate") or 0)
    info.format_name = fmt.get("format_name", "")
    for k, v in (fmt.get("tags") or {}).items():       # key case differs between containers
        if k.lower() == "comment" and str(v).startswith("Trimmer:"):
            info.trimmer_tag = str(v)
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        tags = s.get("tags") or {}
        if not info.trimmer_tag:       # Ogg/Opus keep the comment on the stream, not the container
            for k, v in tags.items():
                if k.lower() == "comment" and str(v).startswith("Trimmer:"):
                    info.trimmer_tag = str(v)
        if kind == "video" and not info.has_video:
            if (s.get("disposition") or {}).get("attached_pic"):
                continue
            info.has_video = True
            info.video_codec = s.get("codec_name", "")
            info.width = int(s.get("width") or 0)
            info.height = int(s.get("height") or 0)
            info.fps = _parse_rate(s.get("avg_frame_rate")) or _parse_rate(s.get("r_frame_rate"))
            info.pix_fmt = s.get("pix_fmt", "")
        elif kind == "audio":
            info.audio.append(AudioTrack(
                index=len(info.audio),
                codec=s.get("codec_name", ""),
                channels=int(s.get("channels") or 0),
                sample_rate=int(s.get("sample_rate") or 0),
                bit_rate=int(s.get("bit_rate") or 0),
                title=_clean_track_title(tags),
                language=tags.get("language"),
            ))
    _cache[key] = info
    return info


def keyframe_times(path, around: float = None, window: float = 15.0) -> List[float]:
    """Video keyframe timestamps (seconds), near `around` or for the whole
    file. Only keyframes can start a stream-copied file, so lossless cuts
    have to land on one of these."""
    cmd = [ffprobe_bin(), "-v", "error", "-select_streams", "v:0"]
    if around is not None:
        start = max(0.0, around - window)
        cmd += ["-read_intervals", f"{start:.3f}%{around + window:.3f}"]
    cmd += ["-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path)]
    try:
        out = run_quiet(cmd)
    except FileNotFoundError:
        return []
    times = []
    for line in out.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                times.append(float(parts[0]))
            except ValueError:
                pass
    return sorted(times)


def keyframe_at_or_before(path, t: float) -> Optional[float]:
    kfs = keyframe_times(path, around=t)
    before = [k for k in kfs if k <= t + 0.001]
    return before[-1] if before else None


# --------------------------------------------------------------------------
# Capability checks (cached)
# --------------------------------------------------------------------------

def ffmpeg_version() -> str:
    try:
        out = run_quiet([ffmpeg_bin(), "-hide_banner", "-version"])
        return out.stdout.splitlines()[0] if out.stdout else "unknown"
    except (FileNotFoundError, IndexError):
        return "not found"


def has_encoder(name: str) -> bool:
    key = ("enc", name)
    if key not in _cache:
        try:
            out = run_quiet([ffmpeg_bin(), "-hide_banner", "-encoders"])
            _cache[key] = any(line.split()[1:2] == [name]
                              for line in out.stdout.splitlines() if line.strip())
        except FileNotFoundError:
            _cache[key] = False
    return _cache[key]


def encoder_works(name: str, pix_fmt: str = "yuv420p", profile: str = None) -> bool:
    """True if ffmpeg lists the encoder AND a one-frame test encode succeeds
    (a hardware encoder is listed even when there's no matching GPU, and a
    GPU or x265 build may not do 10-bit)."""
    key = ("works", name, pix_fmt, profile)
    if key not in _cache:
        ok = False
        if has_encoder(name):
            try:
                out = run_quiet([ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
                                 "-f", "lavfi", "-i", "color=c=black:s=320x240:r=30:d=0.2",
                                 "-frames:v", "3", "-pix_fmt", pix_fmt, "-c:v", name]
                                + (["-profile:v", profile] if profile else []) + ["-f", "null", "-"], timeout=30)
                ok = out.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                ok = False
        _cache[key] = ok
    return _cache[key]


def libvmaf_available() -> bool:
    key = ("filter", "libvmaf")
    if key not in _cache:
        try:
            out = run_quiet([ffmpeg_bin(), "-hide_banner", "-h", "filter=libvmaf"])
            text = (out.stdout or "") + (out.stderr or "")
            _cache[key] = out.returncode == 0 and "Unknown filter" not in text
        except FileNotFoundError:
            _cache[key] = False
    return _cache[key]


# --------------------------------------------------------------------------
# Running ffmpeg with progress
# --------------------------------------------------------------------------

@dataclass
class Progress:
    fraction: Optional[float]   # 0..1, None if unknown
    out_time: float             # seconds of output written
    size: Optional[int]         # bytes written so far
    speed: Optional[float]      # x realtime
    eta: Optional[float]        # seconds


ProgressCallback = Callable[[Progress], None]


@dataclass
class RunResult:
    status: str                 # "ok" | "failed" | "too_large"
    stderr_tail: str = ""
    last_size: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# The commonest ways ffmpeg fails, in words a user can act on. First match wins.
_KNOWN_FAILURES = [
    (r"No space left on device|not enough space on the disk",
     "the disk is full. Free some space where the output is saved and retry."),
    (r"Permission denied|being used by another process|Access is denied",
     "Windows wouldn't let ffmpeg open a file. It may be open in another program, "
     "or the output folder may be read-only."),
    (r"OpenEncodeSessionEx failed|No capable devices found|Cannot load nvcuda|"
     r"Driver does not support the required nvenc API|minimum required Nvidia driver",
     "the NVIDIA encoder couldn't start. Update the graphics driver, or choose Software (CPU) as the encoder."),
    (r"Error initializing an internal MFX session|Error creating a MFX session",
     "Intel Quick Sync couldn't start. Update the graphics driver, or choose Software (CPU) as the encoder."),
    (r"amfrt64\.dll|AMF failed|AMF initialisation failed|AMF initialization failed",
     "the AMD encoder couldn't start. Update the graphics driver, or choose Software (CPU) as the encoder."),
    (r"Invalid data found when processing input|moov atom not found",
     "a source file is damaged or incomplete, for example a recording cut off by a crash."),
    (r"No such file or directory",
     "a file is missing. It may have been moved, renamed or deleted while the job ran."),
    (r"Unknown encoder|Encoder not found",
     "this ffmpeg build doesn't have the encoder these settings need."),
]


def explain_failure(stderr_tail: str) -> str:
    """What went wrong, from ffmpeg's error output: a plain sentence for the
    common failures, else ffmpeg's own last error line."""
    text = stderr_tail or ""
    for pattern, meaning in _KNOWN_FAILURES:
        if re.search(pattern, text, re.IGNORECASE):
            return meaning
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and ln.strip() != "Conversion failed!" and not ln.startswith((" ", "frame=", "size="))]
    return lines[-1][:200] if lines else "ffmpeg stopped with an error."


def failure(what: str, result: "RunResult") -> FFmpegError:
    """The error for a failed run: "<what>: <why>" on the first line (the queue
    shows that), ffmpeg's own output below it for the details and the log."""
    return FFmpegError(f"{what}: {explain_failure(result.stderr_tail)}\n\nffmpeg's output:\n"
                       f"{result.stderr_tail.strip()}")


def _terminate(process):
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    except OSError:
        pass


def run_ffmpeg(args: List[str], duration: float, on_progress: ProgressCallback = None,
               cancel: CancelToken = None, size_limit: int = None,
               output_path: Path = None, cwd=None) -> RunResult:
    """Run ffmpeg (args WITHOUT the leading binary) and report progress.

    - Cancelling (via `cancel`) stops ffmpeg, closes our handle on its log,
      THEN deletes the partial output, and raises Cancelled. That ordering is
      the fix for the Windows self-lock crash in the old compressor.
    - `size_limit`: stop early with status "too_large" the moment the output
      grows past this many bytes (revert-if-larger). The partial output is
      deleted.
    - On failure the partial output is deleted and the stderr tail returned.
    """
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-progress", "pipe:1", "-nostats"] + list(args)
    fd, stderr_name = tempfile.mkstemp(prefix="trimmer_ffmpeg_", suffix=".log")
    os.close(fd)
    stderr_log = Path(stderr_name)

    cancelled = False
    too_large_at = None
    try:
        with open(stderr_log, "w", encoding="utf-8", errors="replace") as errfile:
            try:
                process = _spawn(cmd, stdout=subprocess.PIPE, stderr=errfile,
                                 stdin=subprocess.DEVNULL, cwd=cwd,
                                 text=True, encoding="utf-8", errors="replace",
                                 bufsize=1)
            except FileNotFoundError:
                raise FFmpegError("ffmpeg was not found. Install ffmpeg or set its folder in Settings.")
            if cancel is not None:
                cancel.register(process)        # so Pause can freeze it

            # A reader thread feeds lines into a queue so this loop can check
            # the cancel flag even while ffmpeg is quiet (e.g. seeking).
            lines: "queue.Queue[Optional[str]]" = queue.Queue()

            def reader():
                try:
                    for ln in process.stdout:
                        lines.put(ln)
                finally:
                    lines.put(None)

            threading.Thread(target=reader, daemon=True).start()

            state = {}
            start = time.time()
            last_emit = 0.0
            while True:
                if cancel is not None and cancel.paused:
                    t0 = time.time()
                    while cancel.paused:
                        time.sleep(0.1)
                    start += time.time() - t0       # paused time doesn't count towards the ETA
                if cancel is not None and cancel.cancelled:
                    _terminate(process)
                    cancelled = True
                    break
                try:
                    line = lines.get(timeout=0.15)
                except queue.Empty:
                    continue
                if line is None:
                    process.wait()
                    break
                line = line.strip()
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                state[k] = v
                if k != "progress":
                    continue

                size = None
                if state.get("total_size", "N/A") != "N/A":
                    try:
                        size = int(state["total_size"])
                    except ValueError:
                        pass
                if size_limit is not None and size is not None and size > size_limit:
                    too_large_at = size
                    _terminate(process)
                    break

                now = time.time()
                if on_progress and (v == "end" or now - last_emit >= 0.2):
                    last_emit = now
                    out_t = 0.0
                    us = state.get("out_time_us") or state.get("out_time_ms")
                    try:
                        out_t = max(0.0, int(us) / 1_000_000) if us not in (None, "N/A") else 0.0
                    except ValueError:
                        out_t = 0.0
                    frac = min(1.0, out_t / duration) if duration and duration > 0 else None
                    if v == "end" and duration:
                        frac = 1.0
                    speed = None
                    sp = state.get("speed", "").rstrip("x").strip()
                    try:
                        speed = float(sp) if sp and sp != "N/A" else None
                    except ValueError:
                        pass
                    eta = None
                    if frac and frac > 0.01:
                        elapsed = now - start
                        eta = elapsed / frac - elapsed
                    on_progress(Progress(frac, out_t, size, speed, eta))
        # errfile is closed from here on, so deleting files is safe on Windows.
        if cancel is not None:
            cancel.unregister(process)
        _forget(process)

        if cancelled:
            if output_path is not None:
                safe_unlink(output_path)
            raise Cancelled()
        if too_large_at is not None:
            if output_path is not None:
                safe_unlink(output_path)
            return RunResult("too_large", "", too_large_at)
        if process.returncode != 0:
            tail = stderr_log.read_text(encoding="utf-8", errors="replace")[-1500:]
            if output_path is not None:
                safe_unlink(output_path)
            return RunResult("failed", tail)
        size = Path(output_path).stat().st_size if output_path and Path(output_path).exists() else None
        return RunResult("ok", "", size)
    finally:
        try:
            safe_unlink(stderr_log)
        except OSError:
            pass


def run_checked(args: List[str], cwd=None, cancel: CancelToken = None) -> subprocess.CompletedProcess:
    """Run a short ffmpeg helper command (analysis, probes). Raises
    FFmpegError with ffmpeg's real error text on failure."""
    if cancel is not None:
        cancel.check()
    out = run_quiet([ffmpeg_bin(), "-hide_banner"] + list(args), cwd=cwd, cancel=cancel)
    if cancel is not None:
        cancel.check()
    if out.returncode != 0:
        raise FFmpegError(out.stderr.strip()[-1200:] or f"ffmpeg exited with code {out.returncode}")
    return out
