"""Windows-only helpers for the queue: is a game full screen, a global
pause/resume key, and the pause/resume chimes. Everything here is plain
Win32 through ctypes (no keyboard hooks, nothing that reads other programs'
input), and does nothing on other systems."""

import math
import os
import struct
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, Qt, Signal
from PySide6.QtGui import QKeySequence

from ..core.util import app_data_dir

IS_WINDOWS = sys.platform == "win32"

# Full screen browsers and video players aren't games.
DEFAULT_IGNORE = ("chrome.exe, msedge.exe, firefox.exe, opera.exe, opera_gx.exe, brave.exe, vivaldi.exe, "
                  "vlc.exe, mpv.exe, mpc-hc64.exe, mpc-be64.exe, PotPlayerMini64.exe, Video.UI.exe, "
                  "Microsoft.Media.Player.exe, wmplayer.exe, Netflix.exe, obs64.exe")

# Windows' own full screen surfaces: desktop, taskbar, lock screen, Alt+Tab.
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow",
                  "MultitaskingViewFrame", "XamlExplorerHostIslandWindow", "ForegroundStaging"}


def ignore_list(text: str):
    return {p.strip().lower() for p in (text or "").replace(";", ",").split(",") if p.strip()}


# --------------------------------------------------------------------------
# Full screen detection
# --------------------------------------------------------------------------

def fullscreen_app() -> Optional[str]:
    """The program name (e.g. "game.exe") of the window in front when it
    covers its whole monitor (exclusive full screen or borderless), else None."""
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return None
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    if cls.value in _SHELL_CLASSES:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    mon = user32.MonitorFromWindow(hwnd, 2)          # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return None
    m = mi.rcMonitor
    covers = rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom
    if not covers:
        # Exclusive full screen (Direct3D) can report a smaller window rect.
        state = ctypes.c_int(0)
        try:
            ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        except (AttributeError, OSError):
            return None
        if state.value != 3:                         # QUNS_RUNNING_D3D_FULL_SCREEN
            return None
    return _process_name(pid.value) or "a full screen program"


def _process_name(pid: int) -> str:
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        kernel32.CloseHandle(h)


# --------------------------------------------------------------------------
# Global hotkey
# --------------------------------------------------------------------------

_MODS = ((Qt.ControlModifier, 0x2), (Qt.AltModifier, 0x1), (Qt.ShiftModifier, 0x4), (Qt.MetaModifier, 0x8))
_NAMED_KEYS = {
    Qt.Key_Space: 0x20, Qt.Key_Pause: 0x13, Qt.Key_ScrollLock: 0x91, Qt.Key_Insert: 0x2D, Qt.Key_Delete: 0x2E,
    Qt.Key_Home: 0x24, Qt.Key_End: 0x23, Qt.Key_PageUp: 0x21, Qt.Key_PageDown: 0x22, Qt.Key_Left: 0x25,
    Qt.Key_Up: 0x26, Qt.Key_Right: 0x27, Qt.Key_Down: 0x28, Qt.Key_Print: 0x2C, Qt.Key_Backspace: 0x08,
    Qt.Key_Tab: 0x09, Qt.Key_Return: 0x0D, Qt.Key_Enter: 0x0D, Qt.Key_Minus: 0xBD, Qt.Key_Equal: 0xBB,
    Qt.Key_Comma: 0xBC, Qt.Key_Period: 0xBE, Qt.Key_Slash: 0xBF, Qt.Key_Semicolon: 0xBA,
    Qt.Key_Apostrophe: 0xDE, Qt.Key_BracketLeft: 0xDB, Qt.Key_BracketRight: 0xDD, Qt.Key_Backslash: 0xDC,
    Qt.Key_QuoteLeft: 0xC0, Qt.Key_MediaPlay: 0xB3, Qt.Key_MediaTogglePlayPause: 0xB3,
}


def to_win_hotkey(text: str):
    """(modifiers, virtual key) for RegisterHotKey from text like "Ctrl+Alt+P",
    or None when it isn't one key with any modifiers."""
    seq = QKeySequence(text or "")
    if seq.count() != 1:
        return None
    combo = seq[0]
    key, mods = combo.key(), combo.keyboardModifiers()
    win = 0
    for qt_mod, flag in _MODS:
        if mods & qt_mod:
            win |= flag
    if Qt.Key_A <= key <= Qt.Key_Z or Qt.Key_0 <= key <= Qt.Key_9:
        vk = int(key)                                # same codes as ASCII
    elif Qt.Key_F1 <= key <= Qt.Key_F24:
        vk = 0x70 + int(key) - int(Qt.Key_F1)
    else:
        vk = _NAMED_KEYS.get(key)
    if vk is None:
        return None
    return win, vk


class GlobalHotkey(QObject, QAbstractNativeEventFilter):
    """A key combination that works in every program, even full screen games,
    through Windows' RegisterHotKey. `pressed` fires on the GUI thread."""
    pressed = Signal()
    _ID = 0x7A11

    def __init__(self, parent=None):
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self.text = ""
        self._on = False
        if IS_WINDOWS:
            QCoreApplication.instance().installNativeEventFilter(self)

    def set_key(self, text: str) -> Optional[str]:
        """Use this key (empty = none). Returns a problem to show, or None."""
        self.clear()
        self.text = text or ""
        if not text:
            return None
        parsed = to_win_hotkey(text)
        if parsed is None:
            return f"{text} can't be used as a global key. Pick a letter, number or F-key with Ctrl, Alt or Shift."
        mods, vk = parsed
        if not mods and not (0x70 <= vk <= 0x87 or vk in (0x13, 0x91, 0xB3)):
            return f"{text} alone would stop that key working everywhere else. Add Ctrl, Alt or Shift."
        if not IS_WINDOWS:
            return None
        import ctypes
        if not ctypes.windll.user32.RegisterHotKey(None, self._ID, mods | 0x4000, vk):     # MOD_NOREPEAT
            return f"{text} is already used by another program. Pick another key in Settings."
        self._on = True
        return None

    def clear(self):
        if self._on and IS_WINDOWS:
            import ctypes
            ctypes.windll.user32.UnregisterHotKey(None, self._ID)
        self._on = False

    def nativeEventFilter(self, event_type, message):
        if self._on and event_type == b"windows_generic_MSG":
            from ctypes import wintypes
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312 and msg.wParam == self._ID:      # WM_HOTKEY
                self.pressed.emit()
                return True, 0
        return False, 0


# --------------------------------------------------------------------------
# Chimes
# --------------------------------------------------------------------------

def _chime(freqs, note=0.11, rate=44100) -> bytes:
    """A short soft two-note chime as WAV bytes."""
    frames = bytearray()
    for f in freqs:
        n = int(rate * note)
        for i in range(n):
            env = min(1.0, i / (rate * 0.005)) * math.exp(-4.0 * i / n)      # quick attack, gentle fade
            v = 0.35 * env * (math.sin(2 * math.pi * f * i / rate) + 0.3 * math.sin(4 * math.pi * f * i / rate))
            frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
    data = bytes(frames)
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate,
            rate * 2, 2, 16) + b"data" + struct.pack("<I", len(data)) + data)


CHIMES = {"paused": (784, 523), "resumed": (523, 784)}      # G5 down to C5, and back up


def chime_path(kind: str) -> Path:
    p = app_data_dir() / "sounds" / f"queue {kind}.wav"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_chime(CHIMES[kind]))
    return p


def play_chime(kind: str):
    """'paused' (falling) or 'resumed' (rising), without waiting for it."""
    try:
        path = chime_path(kind)
        if IS_WINDOWS:
            import winsound
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except (OSError, RuntimeError):
        pass


# --------------------------------------------------------------------------
# The queue's guard: game auto-pause and the global key
# --------------------------------------------------------------------------

class QueueGuard(QObject):
    """Pauses the queue while a game is full screen and resumes it after
    (only when it was the one that paused it), and toggles the queue on the
    global key. Both play a chime if Settings say so."""

    def __init__(self, main, check_ms: int = 3000, detect=None):
        super().__init__(main)
        from PySide6.QtCore import QTimer
        self.main = main
        self.detect = detect or fullscreen_app
        self.auto = None            # the program the queue is paused for
        self.override = None        # a program the user chose to keep working through
        self._clear = 0             # checks in a row with no game
        self.hotkey = GlobalHotkey(self)
        self.hotkey.pressed.connect(self.toggle)
        self.timer = QTimer(self)
        self.timer.setInterval(check_ms)
        self.timer.timeout.connect(self.check)

    def apply_settings(self) -> Optional[str]:
        """Start or stop what Settings ask for. Returns a problem with the key, if any."""
        s = self.main.settings
        if s.game_pause and IS_WINDOWS:
            self.timer.start()
        else:
            self.timer.stop()
            self.auto = self.override = None
        return self.hotkey.set_key(s.pause_hotkey)

    def stop(self):
        self.timer.stop()
        self.hotkey.clear()

    def _chime(self, kind):
        if self.main.settings.pause_sounds:
            play_chime(kind)

    def toggle(self):
        """The global key: pause a running queue, resume a paused one, or start waiting jobs."""
        q = self.main.queue
        if q.is_running() and not q.paused:
            q.pause()
            self.auto = None                         # paused by hand: stays paused
            self._chime("paused")
            self.main.show_status("Queue paused (global key).")
        elif q.is_running() or any(i.status == "queued" for i in q.items):
            if self.auto:
                self.override = self.auto            # keep working through this game
            self.auto = None
            q.start_or_resume()
            self._chime("resumed")
            self.main.show_status("Queue resumed (global key).")
        else:
            self.main.show_status("Nothing is waiting in the queue.")

    def check(self):
        q = self.main.queue
        if not q.is_running():
            self.auto = None
        elif self.auto and not q.paused:             # resumed by hand while the game is up
            self.override, self.auto = self.auto, None
        try:
            app = self.detect()
        except OSError:
            app = None
        s = self.main.settings
        if app and app.lower() in ignore_list(s.game_pause_ignore or DEFAULT_IGNORE):
            app = None
        if app is None:
            self.override = None
            self._clear += 1
            if self.auto and q.is_running() and q.paused and self._clear >= 2:
                self.auto = None
                q.start_or_resume()
                self._chime("resumed")
                self.main.show_status("Queue resumed: the game isn't full screen any more.")
            return
        self._clear = 0
        if self.auto or app == self.override:
            return
        if q.is_running() and not q.paused:
            q.pause()
            self.auto = app
            self._chime("paused")
            self.main.show_status(f"Queue paused while {app} is full screen. It carries on when you leave it.")
