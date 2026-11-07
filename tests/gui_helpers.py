"""Shared by the GUI tests: the offscreen app, a data folder of their own, pump() to let Qt run,
window() to open the real main window, and the clips several tests open. Importing this sets them
up (once)."""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

WORK = Path(os.environ.get("TRIMMER_TEST_DIR", tempfile.gettempdir())) / "trimmer_gui_tests"
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)
os.environ["TRIMMER_DATA_DIR"] = str(WORK / "appdata")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from tests import fixtures as fx  # noqa: E402
from trimmer.core.settings import AppSettings  # noqa: E402
from trimmer.gui.main_window import MainWindow  # noqa: E402

app = QApplication.instance() or QApplication([])
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
# The queue warnings open a real dialog; these older tests queue the same file
# twice on purpose, so they answer "add anyway" (tests/test_warnings.py covers the dialogs).
from trimmer.gui import queue_checks  # noqa: E402
queue_checks.ask = lambda *a, **k: ("anyway", False)


def pump(sec, cond=lambda: False):
    end = time.time() + sec
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.02)


def window(out):
    """The main window as Trimmer opens it, saving into `out`, once its startup checks are done."""
    s = AppSettings.load()
    s.output_dir = str(out)
    w = MainWindow(s)
    w.show()
    w.startup_checks()
    pump(5, lambda: not any(t.isRunning() for t in w.tasks))
    pump(0.1)
    return w


# The clips most GUI tests open, made the first time one asks, so any test can run on its own.
def gameplay_clip() -> Path:
    """src/Gameplay clip.mp4: 20 s of the OBS stream, 2 audio tracks."""
    p = WORK / "src" / "Gameplay clip.mp4"
    return p if p.exists() else fx.single_clip(p.parent)


def obs_stream() -> Path:
    """src2/obs_stream.mp4: the whole 70 s OBS stream."""
    return fx.long_encode(WORK / "src2")


def replays() -> Path:
    """The rep folder: five replays cut from the OBS stream, in two sessions (4 + 1)."""
    d = WORK / "rep"
    if not d.exists():
        fx.shared_replays(d)
    return d
