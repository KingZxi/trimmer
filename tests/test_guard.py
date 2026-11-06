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


def test_about_tab_credits_the_author():
    from PySide6.QtWidgets import QLabel, QTabWidget
    from trimmer.gui.settings_dialog import SettingsDialog
    d = SettingsDialog(AppSettings(), ["software"])
    tabs = d.findChild(QTabWidget)
    about = tabs.widget(tabs.count() - 1)
    assert tabs.tabText(tabs.count() - 1) == "About"
    text = " ".join(l.text() for l in about.findChildren(QLabel))
    assert "KingZxi" in text and "https://github.com/KingZxi" in text


def test_ffmpeg_found_in_a_folder_beside_the_app(tmp_path, monkeypatch):
    from trimmer.core import ffmpeg as ff
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    (tmp_path / "ffmpeg" / "bin").mkdir(parents=True)
    (tmp_path / "ffmpeg" / "bin" / exe).write_bytes(b"")
    monkeypatch.setattr(ff, "_beside_app", lambda: [tmp_path / "ffmpeg" / "bin", tmp_path / "ffmpeg"])
    assert ff.ffmpeg_bin() == str(tmp_path / "ffmpeg" / "bin" / exe)


def test_first_launch_without_ffmpeg_explains_and_lets_you_pick(monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from trimmer.gui import main_window as mw
    shown = []

    def fake_exec(box):
        shown.append(box.text())
        box.buttons()[-1].click()            # "Later"
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    w = mw.MainWindow(AppSettings())
    assert w.find_ffmpeg() is False
    assert "winget install Gyan.FFmpeg" in shown[0]
    w.close()


def test_version_and_licence():
    import trimmer
    assert trimmer.__version__.count(".") == 2 and trimmer.LICENSE_NAME == "GPL-3.0"
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Version 3, 29 June 2007" in text and "END OF TERMS AND CONDITIONS" in text


def test_old_logs_are_deleted_and_can_be_cleared(monkeypatch):
    import time
    from trimmer.core import journal
    from trimmer.core.util import logs_dir
    folder = logs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for p in folder.iterdir():
        p.unlink()
    old, new, crash = (folder / n for n in ("log (2026-01-01 10-00-00).txt", "log (2026-11-01 10-00-00).txt",
                                             "crash (2026-01-02 10-00-00).txt"))
    other = folder / "notes.txt"                   # not Trimmer's: never touched
    for p in (old, new, crash, other):
        p.write_text("x")
    month_ago = time.time() - 40 * 86400
    for p in (old, crash, other):
        os.utime(p, (month_ago, month_ago))
    assert journal.clean_logs(30) == 2
    assert not old.exists() and not crash.exists() and new.exists() and other.exists()

    from trimmer.gui import settings_dialog
    from trimmer.gui.settings_dialog import SettingsDialog
    monkeypatch.setattr(settings_dialog.QMessageBox, "question", lambda *a: settings_dialog.QMessageBox.Yes)
    monkeypatch.setattr(settings_dialog.QMessageBox, "information", lambda *a: None)
    d = SettingsDialog(AppSettings(), ["software"])
    assert d.auto_logs.isChecked() and d.keep_logs.value() == 30
    d.b_clear_logs.click()
    assert not new.exists() and other.exists()
    d.auto_logs.setChecked(False)
    assert not d.keep_logs.isEnabled()
    d.keep_logs.setValue(90)
    d._save()
    back = AppSettings.load()
    assert not back.auto_delete_logs and back.keep_logs_days == 90
    other.unlink()


def test_settings_rows_off_their_default_get_a_restore_button():
    from trimmer.gui.settings_dialog import SettingsDialog
    s = AppSettings()
    s.conflict_policy, s.max_gap, s.keep_logs_days = "rename", 3.0, 60
    d = SettingsDialog(s, ["software"])
    shown = sorted(k for k, b in d.resets.items() if not b.isHidden())
    assert shown == ["auto_logs", "conflict", "gap", "keep_logs"]     # the logs row holds both controls
    d.resets["gap"].click()
    assert d.gap.value() == 1.5 and d.resets["gap"].isHidden()
    d.tags.setChecked(False)                                           # changing a row shows its button
    assert not d.resets["tags"].isHidden()
    assert d.b_enc_default.isHidden()
    d.enc.crf.setValue(30)
    assert not d.b_enc_default.isHidden()
    d.b_enc_default.click()
    assert d.enc.settings().quality == 23 and d.b_enc_default.isHidden()


def test_auto3_preset_is_added_once(tmp_path, monkeypatch):
    import json
    from trimmer.core import settings as st
    monkeypatch.setattr(st.AppSettings, "path", classmethod(lambda cls: tmp_path / "settings.json"))
    (tmp_path / "settings.json").write_text(json.dumps({"presets": {"Mine": {"codec": "h264"}}}))
    s = AppSettings.load()
    assert list(s.presets) == ["Mine", st.AUTO3_PRESET]
    auto3 = s.presets[st.AUTO3_PRESET]
    assert auto3["quality"] == "auto3" and auto3["auto3_target"] == 93.0 and auto3["revert_if_larger"]
    del s.presets[st.AUTO3_PRESET]
    s.save()
    assert st.AUTO3_PRESET not in AppSettings.load().presets          # deleting it sticks


def test_playback_check_reports_why_it_failed(tmp_path):
    """Trimmer --check-playback (the build's self test) and Logs\\playback.txt say why there's no sound."""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "TRIMMER_DATA_DIR": str(tmp_path / "data")}
    blocked = ("import sys, runpy; sys.modules['PySide6.QtMultimedia'] = None; "
               "sys.argv = ['trimmer.py', '--check-playback', sys.argv[1]]; "
               "runpy.run_path('trimmer.py', run_name='__main__')")
    out = tmp_path / "check.txt"
    r = subprocess.run([sys.executable, "-c", blocked, str(out)], cwd=root, env=env, timeout=120)
    text = out.read_text(encoding="utf-8")
    assert r.returncode == 1 and "Playback: FAILED" in text and "QtMultimedia" in text
    code = ("import sys, os; sys.modules['PySide6.QtMultimedia'] = None; "
            "from trimmer.gui import playback as pb; from trimmer.core.journal import log_files; "
            "p = pb.write_report(); print(p.name, p.name in [f.name for f in log_files()], pb.NO_SOUND)")
    r = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.stdout.split()[:2] == ["playback.txt", "True"], r.stderr


def test_totals_and_settings_survive_a_damaged_file(tmp_path, monkeypatch):
    """A crash mid-write could leave stats.json or settings.json empty: the previous copy is used."""
    from trimmer.core import savings
    from trimmer.core.settings import AppSettings
    monkeypatch.setenv("TRIMMER_DATA_DIR", str(tmp_path))
    s = savings.Savings(saved_bytes=5_000_000, jobs=3, clips=4)
    savings._save(s)
    s.jobs = 4
    savings._save(s)                           # the 3-job copy becomes stats.json.bak
    savings.path().write_text("", encoding="utf-8")      # what a power cut can leave behind
    assert savings.load().jobs == 3 and savings.load().saved_bytes == 5_000_000
    savings.path().write_text('{"jobs": 4', encoding="utf-8")   # half written
    assert savings.load().jobs == 3
    a = AppSettings()
    a.theme = "light"
    a.save()
    a.save()
    AppSettings.path().write_text("", encoding="utf-8")
    assert AppSettings.load().theme == "light"
    assert not list(tmp_path.glob("*.tmp"))
