"""Shared test setup.

Loaded by pytest before any test file, so every test uses a throwaway data folder
and can never touch the real settings, queue or stats in %APPDATA%\\Trimmer."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TRIMMER_DATA_DIR", tempfile.mkdtemp(prefix="trimmer_data_"))

WORK = Path(os.environ.get("TRIMMER_TEST_DIR", tempfile.gettempdir())) / "trimmer_tests"


@pytest.fixture(scope="session")
def work():
    """The shared folder engine tests build their clips and outputs in."""
    WORK.mkdir(parents=True, exist_ok=True)
    return WORK


@pytest.fixture(autouse=True)
def close_dialog(request):
    """Each GUI test starts from a clean data folder, closing never opens the real Save/Close
    dialog, and leftovers don't leak into the next test.
    (Patched by hand: a test's monkeypatch.undo() would take a fixture's patch away too.)"""
    if request.module.__name__ not in ("tests.test_gui", "tests.test_warnings", "tests.test_lists", "tests.test_updates"):
        yield                       # engine tests: no window, and never touch the real app data
        return
    from trimmer.gui import main_window
    from trimmer.core.journal import clear_queue
    from trimmer.core.session import autosave_path, clear_session
    from trimmer.core.util import app_data_dir
    from tests.gui_helpers import WORK
    # Every GUI test starts like a first run: default settings and totals, no queue or saved state.
    assert WORK in app_data_dir().parents           # the tests' own data folder, never the real one
    for f in app_data_dir().glob("*.json*"):
        f.unlink()
    real = main_window.ask_close_choice
    main_window.ask_close_choice = lambda parent: ("save", False)
    main_window.MainWindow.AUTOSAVE_MS = 0      # tests call autosave() themselves; no stray timers
    yield
    # Windows a test left open are closed and freed, so later tests (and theme changes) don't
    # have to restyle all of them.
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    for w in QApplication.topLevelWidgets():
        w.close()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    main_window.ask_close_choice = real
    clear_session()
    clear_session(autosave_path())
    clear_queue()


@pytest.fixture(autouse=True)
def no_slot_errors(request):
    """An exception inside a Qt slot only reaches sys.excepthook (the app shows its error box);
    fail the test instead of letting it pass silently."""
    if request.module.__name__ not in ("tests.test_gui", "tests.test_warnings", "tests.test_lists", "tests.test_updates"):
        yield
        return
    errors, real = [], sys.excepthook
    sys.excepthook = lambda *exc: errors.append(exc)
    yield
    sys.excepthook = real
    if errors:
        import traceback
        raise AssertionError("Error in a Qt slot:\n" + "".join(traceback.format_exception(*errors[0])))
