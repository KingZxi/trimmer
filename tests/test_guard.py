"""The queue's guard: pausing for full screen games, the global key and its chimes.
The Windows calls can't run here, so the game check is swapped for a stand-in."""

import os
import sys
import tempfile
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("TRIMMER_DATA_DIR", str(Path(tempfile.gettempdir()) / "trimmer_guard_tests"))

from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from trimmer.core.settings import AppSettings  # noqa: E402
from trimmer.gui import system_hooks as sh  # noqa: E402

app = QApplication.instance() or QApplication([])


class Item:
    def __init__(self, status):
        self.status = status


class FakeQueue:
    def __init__(self, running=True, waiting=0):
        self.running, self.paused = running, False
        self.items = [Item("queued") for _ in range(waiting)]

    def is_running(self):
        return self.running

    def pause(self):
        self.paused = True

    def start_or_resume(self):
        self.running, self.paused = True, False


class FakeMain(QObject):
    def __init__(self, queue, **settings):
        super().__init__()
        self.queue = queue
        self.settings = AppSettings(**settings)
        self.messages = []

    def show_status(self, text):
        self.messages.append(text)


def guard(queue, game=None, **settings):
    main = FakeMain(queue, **settings)
    state = {"app": game}
    g = sh.QueueGuard(main, detect=lambda: state["app"])
    chimes = []
    g._chime = chimes.append
    return g, state, chimes


def test_game_pauses_and_resumes_after_two_clear_checks():
    q = FakeQueue()
    g, state, chimes = guard(q, "game.exe")
    g.check()
    assert q.paused and g.auto == "game.exe" and chimes == ["paused"]
    g.check()                                   # still in the game: nothing new
    assert chimes == ["paused"]
    state["app"] = None
    g.check()
    assert q.paused                             # one clear check isn't enough (alt-tab, loading screens)
    g.check()
    assert not q.paused and chimes == ["paused", "resumed"]


def test_ignored_programs_and_idle_queue_do_nothing():
    q = FakeQueue()
    g, state, chimes = guard(q, "Chrome.exe")
    g.check()
    assert not q.paused and not chimes
    state["app"] = "vlc.exe"
    g.main.settings.game_pause_ignore = "game.exe"   # own list replaces the built-in one
    g.check()
    assert q.paused
    idle = FakeQueue(running=False)
    g2, _, chimes2 = guard(idle, "game.exe")
    g2.check()
    assert not idle.paused and not chimes2


def test_manual_pause_is_never_undone():
    q = FakeQueue()
    q.paused = True                             # the user paused it before the game started
    g, state, chimes = guard(q, "game.exe")
    g.check()
    state["app"] = None
    g.check(); g.check()
    assert q.paused and not chimes


def test_manual_resume_during_a_game_sticks_until_it_closes():
    q = FakeQueue()
    g, state, chimes = guard(q, "game.exe")
    g.check()
    q.start_or_resume()                         # resumed by hand with the game still up
    g.check(); g.check()
    assert not q.paused and g.override == "game.exe"
    state["app"] = None
    g.check()
    assert g.override is None
    state["app"] = "game.exe"                   # a new session of the game pauses again
    g.check()
    assert q.paused


def test_toggle_pauses_resumes_and_starts():
    q = FakeQueue()
    g, state, chimes = guard(q, "game.exe")
    g.toggle()
    assert q.paused and chimes == ["paused"]
    state["app"] = None
    g.check(); g.check()
    assert q.paused                             # paused by the key: only the key or Resume carries on
    g.toggle()
    assert not q.paused and chimes == ["paused", "resumed"]

    q2 = FakeQueue()
    g2, _, _ = guard(q2, "game.exe")
    g2.check()
    g2.toggle()                                 # key during a game: work through it
    g2.check()
    assert not q2.paused

    idle = FakeQueue(running=False, waiting=1)
    g3, _, chimes3 = guard(idle)
    g3.toggle()
    assert idle.running and chimes3 == ["resumed"]
    empty = FakeQueue(running=False)
    g4, _, chimes4 = guard(empty)
    g4.toggle()
    assert not empty.running and not chimes4 and "Nothing" in g4.main.messages[-1]


def test_hotkey_parsing():
    assert sh.to_win_hotkey("Ctrl+Alt+P") == (3, 0x50)
    assert sh.to_win_hotkey("Ctrl+Shift+F9") == (6, 0x78)
    assert sh.to_win_hotkey("Alt+Pause") == (1, 0x13)
    assert sh.to_win_hotkey("Ctrl+A, Ctrl+B") is None
    assert sh.to_win_hotkey("") is None


def test_bare_letter_key_refused():
    h = sh.GlobalHotkey()
    assert "Ctrl" in (h.set_key("P") or "")
    assert h.set_key("") is None


def test_chimes_are_valid_and_distinct():
    a, b = sh.chime_path("paused"), sh.chime_path("resumed")
    for p in (a, b):
        with wave.open(str(p)) as w:
            assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 44100
            assert 0.15 < w.getnframes() / 44100 < 0.4
    assert a.read_bytes() != b.read_bytes()
    sh.play_chime("paused")                     # silent off Windows, must not raise


def test_settings_dialog_saves_queue_options():
    from PySide6.QtGui import QKeySequence
    from trimmer.gui.settings_dialog import SettingsDialog
    s = AppSettings()
    d = SettingsDialog(s, ["software"])
    assert d.game_ignore.text() == sh.DEFAULT_IGNORE and d.hotkey_edit.keySequence().toString() == "Ctrl+Alt+P"
    d.game_pause.setChecked(True)
    d.hotkey_edit.setKeySequence(QKeySequence("Ctrl+Shift+F9"))
    d.sounds.setChecked(False)
    d._save()
    back = AppSettings.load()
    assert back.game_pause and back.pause_hotkey == "Ctrl+Shift+F9" and not back.pause_sounds
    assert back.game_pause_ignore == ""          # unchanged list follows the built-in one
