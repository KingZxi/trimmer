"""A small video viewer window, used by Replay merge to look inside a clip
or to watch a merged preview of a whole session."""

import shutil

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QDialog, QPushButton, QVBoxLayout, QWidget

from ..core import peek
from ..core.util import format_hms_ms
from .audio_device import follow_default_output
from .theme import set_kind
from .widgets import FullScreen, Timeline, TrackPicker, card, hbox, label
from .worker import Task

# Qt's player, or the silent frame fallback when Windows blocks its DLLs
from .playback import (HAVE_PLAYBACK, NO_SOUND, NO_SOUND_TIP, PLAYBACK_ERROR, QAudioOutput, QMediaPlayer,
                       QVideoWidget)


class PreviewDialog(QDialog):
    """Plays one file. `marks` are times (s) drawn on the timeline, e.g. joins.
    `info` (MediaInfo) names the audio tracks; `temp_dir` is deleted on close.
    set_used() highlights the part of the clip a merge will use."""
    position = Signal(float)        # seconds into the file, as it plays or is scrubbed
    closed = Signal()

    def __init__(self, main, path, title, info=None, marks=(), note="", temp_dir=None, parent=None):
        super().__init__(parent)
        self.path = str(path)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(980, 680)
        self.info = info
        self.temp_dir = temp_dir
        self.marks = list(marks)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        lay.addWidget(label(title, "h2"))
        if note:
            lay.addWidget(label(note, "muted", wrap=True))

        # Video, timeline and controls sit together so they can go full screen as one.
        self.stage = QWidget()
        stage = QVBoxLayout(self.stage)
        stage.setContentsMargins(0, 0, 0, 0)
        stage.setSpacing(10)
        lay.addWidget(self.stage, 1)
        pv, pvl = card(margins=(0, 0, 0, 0), spacing=0)
        pv.setObjectName("video")
        self.video = QVideoWidget()
        self.video.setMinimumHeight(300)
        pvl.addWidget(self.video)
        stage.addWidget(pv, 1)
        self.player = QMediaPlayer(self)
        if QAudioOutput is not None:
            self.audio_out = QAudioOutput(self)
            self.player.setAudioOutput(self.audio_out)
            follow_default_output(self.audio_out)     # don't stay on the device it started on
        self.player.setVideoOutput(self.video)

        self.timeline = Timeline()
        self.timeline.setToolTip("Click or drag to seek. Dashed lines mark where clips join.")
        self.timeline.seek.connect(lambda t: self.player.setPosition(int(t * 1000)))
        stage.addWidget(self.timeline)
        self.used_note = label("", "muted", wrap=True)
        self.used_note.hide()
        stage.addWidget(self.used_note)

        self.b_play = QPushButton("Play")
        self.b_play.setMinimumWidth(76)
        self.b_play.clicked.connect(self.toggle_play)
        b_back = set_kind(QPushButton("‹"), "icon")
        b_back.clicked.connect(lambda: self.step(-1))
        b_fwd = set_kind(QPushButton("›"), "icon")
        b_fwd.clicked.connect(lambda: self.step(1))
        self.time_label = label("0:00.000", "mono")
        self.tracks = TrackPicker(main.track_prefs)
        self.tracks.track_chosen.connect(self.player.setActiveAudioTrack)
        hear = label("Hear", "muted")
        b_full = QPushButton("Full screen")
        b_full.setToolTip("Full screen  (F11 or double-click the video; Esc to leave)")
        b_full.clicked.connect(lambda: self.full.toggle())
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.close)
        no_sound = label(NO_SOUND, "muted")
        no_sound.setToolTip(NO_SOUND_TIP + PLAYBACK_ERROR)
        stage.addWidget(hbox(self.b_play, b_back, b_fwd, self.time_label, "stretch", no_sound, hear, self.tracks,
                             b_full, b_close, stretch_end=False))
        if HAVE_PLAYBACK:
            no_sound.hide()
        else:
            hear.hide()
            self.tracks.hide()

        self.player.positionChanged.connect(self._pos)
        self.player.durationChanged.connect(self._dur)
        self.player.tracksChanged.connect(self._tracks_changed)
        keys = (("Space", self.toggle_play), ("Left", lambda: self.step(-1)),
                ("Right", lambda: self.step(1)), ("J", lambda: self.jump(-5)), ("L", lambda: self.jump(5)))
        self.full = FullScreen(self.stage, self.video, keys, title, parent=self)
        for key, fn in keys + (("F11", lambda: self.full.toggle()),):
            QShortcut(QKeySequence(key), self, activated=fn)
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        if info is not None:
            self.timeline.set_state(duration=info.duration, snaps=self.marks)
        self._open = True
        self._load_peek(main)
        QTimer.singleShot(0, self.toggle_play)

    def _load_peek(self, main):
        """Waveform and thumbnails on the timeline, as in the Clips tab. The look-ups belong to the
        main window, which waits for them on quit; this window may be closed before they finish."""
        info = self.info
        if not main.settings.show_peek or info is None or not info.duration:
            return
        path = self.path
        jobs = []
        if info.audio:
            jobs.append((lambda: peek.waveform(path), lambda w: self.timeline.set_peek(waveform=w)))
        if info.has_video:
            count = max(4, min(60, self.timeline.width() // 80))
            jobs.append((lambda: peek.thumbnails(path, info.duration, count),
                         lambda th: self.timeline.set_peek(thumbs=th)))
        for work, show in jobs:
            t = Task(work, parent=main)
            t.done.connect(lambda r, show=show: self._open and show(r))
            main.tasks.append(t)
            t.start()

    def _dur(self, ms):
        self.timeline.set_state(duration=ms / 1000, snaps=self.marks)

    def set_used(self, rng, text):
        """Highlight `rng` (start, end) on the timeline, or nothing if None."""
        self.timeline.set_state(segments=[rng] if rng else [])
        self.used_note.setText(text)
        self.used_note.setVisible(bool(text))

    def _pos(self, ms):
        t = ms / 1000
        self.position.emit(t)
        self.timeline.set_state(position=t)
        self.time_label.setText(f"{format_hms_ms(t)}  /  {format_hms_ms(self.timeline.duration)}")

    def _tracks_changed(self):
        n = len(self.player.audioTracks())
        audio = self.info.audio if self.info is not None else []
        labels = [audio[i].label if i < len(audio) else f"Track {i + 1}" for i in range(n)]
        titles = [(audio[i].title or audio[i].label) if i < len(audio) else labels[i] for i in range(n)]
        self.tracks.set_tracks(labels, titles)

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.b_play.setText("Play")
        else:
            self.player.play()
            self.b_play.setText("Pause")

    def step(self, frames):
        self.player.pause()
        self.b_play.setText("Play")
        fps = (self.info.fps if self.info is not None else 0) or 30
        self.player.setPosition(max(0, int(self.player.position() + frames * 1000 / fps)))

    def jump(self, seconds):
        self.player.setPosition(max(0, int(self.player.position() + seconds * 1000)))

    def _shut(self):
        """Stop and let go, once. Esc closes a dialog through done(), which deletes
        it without a closeEvent, so both paths come here: otherwise Replay merge
        keeps the dead viewer and later updates hit its deleted timeline."""
        if not self._open:
            return
        self._open = False
        self.full.leave()
        self.closed.emit()
        self.player.stop()
        self.player.setSource(QUrl())       # release the file before deleting it
        if self.temp_dir:
            d = self.temp_dir
            QTimer.singleShot(500, lambda: shutil.rmtree(d, ignore_errors=True))

    def done(self, result):
        self._shut()
        super().done(result)

    def closeEvent(self, e):
        self._shut()
        super().closeEvent(e)
