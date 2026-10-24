"""Stand-in preview for when Qt's multimedia module can't load (for example
when Windows Smart App Control blocks its unsigned DLLs).

It has the same small interface the clips tab uses from QMediaPlayer, but
shows still frames that ffmpeg grabs, with no sound. Trimming works the
same way."""

import subprocess
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy

from ..core import ffmpeg as ff


class FrameView(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setObjectName("muted")
        self.setMargin(24)
        self.setText("Preview shows still frames (video playback isn't available on this PC)")
        self.setWordWrap(True)
        self._pix = None

    def show_jpeg(self, data: bytes):
        pix = QPixmap()
        if data and pix.loadFromData(data, "JPG"):
            self._pix = pix
            self._rescale()

    def clear_frame(self):
        self._pix = None
        self.clear()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._rescale()

    def _rescale(self):
        if self._pix is not None:
            self.setPixmap(self._pix.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


class FramePlayer(QObject):
    StoppedState, PlayingState, PausedState = 0, 1, 2
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    tracksChanged = Signal()
    _frame = Signal(bytes, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = None
        self.view = None
        self._pos = 0
        self._dur = 0
        self._state = self.StoppedState
        self._want = None             # (path, ms) still to be grabbed; latest wins
        self._cond = threading.Condition()
        self._frame.connect(self._show)
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)
        self._last_tick = 0.0
        threading.Thread(target=self._grabber, daemon=True).start()

    # QMediaPlayer-compatible bits used by the clips tab
    def setAudioOutput(self, _):
        pass

    def setVideoOutput(self, view):
        self.view = view

    def audioTracks(self):
        return []

    def setActiveAudioTrack(self, _):
        pass

    def playbackState(self):
        return self._state

    def position(self):
        return self._pos

    def setSource(self, url):
        self.stop()
        path = url.toLocalFile() if url and not url.isEmpty() else None
        self.path = path
        self._pos = 0
        self._dur = 0
        if self.view:
            self.view.clear_frame()
        if path:
            try:
                self._dur = int(ff.probe(path).duration * 1000)
            except Exception:
                self._dur = 0
            self.durationChanged.emit(self._dur)
            self._request()
        self.positionChanged.emit(0)

    def setPosition(self, ms):
        self._pos = max(0, min(int(ms), self._dur or int(ms)))
        self.positionChanged.emit(self._pos)
        self._request()

    def play(self):
        if self.path:
            self._state = self.PlayingState
            self._last_tick = time.monotonic()
            self._timer.start()

    def pause(self):
        if self._state == self.PlayingState:
            self._state = self.PausedState
        self._timer.stop()

    def stop(self):
        self._state = self.StoppedState
        self._timer.stop()

    # internals
    def _tick(self):
        now = time.monotonic()
        self._pos += int((now - self._last_tick) * 1000)
        self._last_tick = now
        if self._dur and self._pos >= self._dur:
            self._pos = self._dur
            self.pause()
        self.positionChanged.emit(self._pos)
        self._request()

    def _request(self):
        if self.path:
            with self._cond:
                self._want = (self.path, self._pos)
                self._cond.notify()

    def _grabber(self):
        while True:
            with self._cond:
                while self._want is None:
                    self._cond.wait()
                path, ms = self._want
                self._want = None
            try:
                out = subprocess.run(
                    [ff.ffmpeg_bin(), "-v", "error", "-ss", f"{ms / 1000:.3f}", "-i", path,
                     "-frames:v", "1", "-vf", "scale=-2:540", "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                    capture_output=True, timeout=20, creationflags=ff._NO_WINDOW)
                data = out.stdout
            except (OSError, subprocess.TimeoutExpired):
                data = b""
            self._frame.emit(data, ms)

    def _show(self, data, _ms):
        if self.view is not None:
            self.view.show_jpeg(data)
