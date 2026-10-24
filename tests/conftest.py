"""Shared GUI test setup."""

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
    from trimmer.core.session import clear_session
    real = main_window.ask_close_choice
    main_window.ask_close_choice = lambda parent: ("save", False)
    yield
    main_window.ask_close_choice = real
    clear_session()
    clear_queue()
