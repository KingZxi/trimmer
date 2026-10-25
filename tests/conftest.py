"""Shared GUI test setup."""

import sys

import pytest


@pytest.fixture(autouse=True)
def close_dialog(request):
    """Closing never opens the real Save/Close dialog; leftovers don't leak into the next test.
    (Patched by hand: a test's monkeypatch.undo() would take a fixture's patch away too.)"""
    if request.module.__name__ not in ("tests.test_gui", "tests.test_warnings"):
        yield                       # engine tests: no window, and never touch the real app data
        return
    from trimmer.gui import main_window
    from trimmer.core.journal import clear_queue
    from trimmer.core.session import autosave_path, clear_session
    real = main_window.ask_close_choice
    main_window.ask_close_choice = lambda parent: ("save", False)
    main_window.MainWindow.AUTOSAVE_MS = 0      # tests call autosave() themselves; no stray timers
    yield
    main_window.ask_close_choice = real
    clear_session()
    clear_session(autosave_path())
    clear_queue()


@pytest.fixture(autouse=True)
def no_slot_errors(request):
    """An exception inside a Qt slot only reaches sys.excepthook (the app shows its error box);
    fail the test instead of letting it pass silently."""
    if request.module.__name__ not in ("tests.test_gui", "tests.test_warnings"):
        yield
        return
    errors, real = [], sys.excepthook
    sys.excepthook = lambda *exc: errors.append(exc)
    yield
    sys.excepthook = real
    if errors:
        import traceback
        raise AssertionError("Error in a Qt slot:\n" + "".join(traceback.format_exception(*errors[0])))
