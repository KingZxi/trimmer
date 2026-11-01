"""No ffmpeg outlives Trimmer: cancelling stops quality trials at once, and
quitting (or the app's process ending) ends every ffmpeg it started."""

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trimmer.core import ffmpeg as ff  # noqa: E402

pytestmark = pytest.mark.skipif(ff.tools_available(), reason="ffmpeg not installed")

# A few minutes of work for ffmpeg: long enough that it is surely still running.
LONG = ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=60", "-t", "600",
        "-c:v", "libx264", "-preset", "veryslow", "-f", "null", "-"]


def _alive(pid):
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
        return str(pid) in out.stdout
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
    except OSError:
        return False
    return state != "Z"


def _wait_for_child(before):
    for _ in range(100):
        with ff._children_lock:
            new = [p for p in ff._children if p not in before]
        if new:
            return new[0]
        time.sleep(0.05)
    raise AssertionError("ffmpeg did not start")


def test_cancel_stops_a_running_helper_at_once():
    token = ff.CancelToken()
    caught = []

    def work():
        try:
            ff.run_checked(LONG, cancel=token)
        except BaseException as exc:        # noqa: BLE001
            caught.append(exc)

    before = set(ff._children)
    t = threading.Thread(target=work)
    t.start()
    proc = _wait_for_child(before)
    token.cancel()
    t.join(5)
    assert not t.is_alive()
    assert caught and isinstance(caught[0], ff.Cancelled)
    assert proc.poll() is not None and not _alive(proc.pid)
    assert proc not in ff._children


def test_kill_all_ends_helpers_and_jobs():
    results = {}
    before = set(ff._children)
    a = threading.Thread(target=lambda: results.update(quiet=ff.run_quiet([ff.ffmpeg_bin()] + LONG)))
    b = threading.Thread(target=lambda: results.update(job=ff.run_ffmpeg(LONG, 600)))
    a.start()
    _wait_for_child(before)
    b.start()
    for _ in range(100):
        if len(set(ff._children) - before) >= 2:
            break
        time.sleep(0.05)
    procs = list(set(ff._children) - before)
    assert ff.kill_all() == 2
    a.join(5)
    b.join(5)
    assert not a.is_alive() and not b.is_alive()
    assert results["quiet"].returncode != 0
    assert not results["job"].ok
    assert all(not _alive(p.pid) for p in procs)


def test_no_ffmpeg_left_after_the_app_exits():
    """A process that quits (or crashes) mid-encode takes its ffmpeg with it."""
    script = (
        "import sys, threading, time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from trimmer.core import ffmpeg as ff\n"
        f"LONG = {LONG!r}\n"
        "threading.Thread(target=lambda: ff.run_ffmpeg(LONG, 600), daemon=True).start()\n"
        "threading.Thread(target=lambda: ff.run_quiet([ff.ffmpeg_bin()] + LONG), daemon=True).start()\n"
        "while len(ff._children) < 2: time.sleep(0.05)\n"
        "print(' '.join(str(p.pid) for p in ff._children), flush=True)\n"
        "sys.exit(0)\n")
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    pids = [int(x) for x in out.stdout.split()]
    assert len(pids) == 2, out.stderr
    for _ in range(50):
        if not any(_alive(p) for p in pids):
            break
        time.sleep(0.1)
    assert not any(_alive(p) for p in pids)


def test_nothing_new_starts_while_quitting(monkeypatch):
    monkeypatch.setattr(ff, "_closing", True)
    out = ff.run_quiet([ff.ffmpeg_bin(), "-version"])
    assert out.returncode != 0
    with pytest.raises(ff.Cancelled):
        ff.run_ffmpeg(LONG, 600)
