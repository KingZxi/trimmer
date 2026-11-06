"""Reusable widgets: the trim timeline, the compression settings form and
the dark theme."""

import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QIcon, QImage, QKeySequence, QPainter, QPainterPath,
                           QPen, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QMenu, QMessageBox, QSlider, QSpinBox, QTableWidget, QToolButton, QTreeWidget,
                               QVBoxLayout, QWidget)

from ..core.encode import CODECS, ENCODERS, VALID_PRESETS, EncodeSettings, give_up_target
from ..core.util import format_hms_ms

from .theme import apply_theme, clear_icon_path, colour  # noqa: F401  (re-exported)


def reveal(path: Path):
    """Show a file in Explorer with it selected (or its folder elsewhere)."""
    path = Path(path)
    if sys.platform == "win32" and path.exists():
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent if path.is_file() else path)))


def show_in_folder(parent, path) -> bool:
    """Open the file's folder with the file selected. If it's gone, say so and
    offer the folder it was in (when that still exists)."""
    path = Path(path)
    if path.exists():
        reveal(path)
        return True
    folder = path.parent
    if folder.is_dir():
        if QMessageBox.question(parent, "File not found",
                                f"{path.name} no longer exists. It was moved, renamed or deleted.\n\n"
                                f"Open the folder it was in?\n{folder}",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) == QMessageBox.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
    else:
        QMessageBox.information(parent, "File not found",
                                f"{path.name} no longer exists, and neither does its folder:\n{folder}")
    return False


class _Hint:
    """Paints a centred hint over an empty view (e.g. "Drop clips here")."""
    hint = ""
    sub_hint = ""
    easy_deselect = False       # Esc, or a click on empty space, clears the selection

    def keyPressEvent(self, e):
        if (self.easy_deselect and e.key() == Qt.Key_Escape
                and self.state() != QAbstractItemView.EditingState):
            self.selectionModel().clear()
            return
        super().keyPressEvent(e)

    def mousePressEvent(self, e):
        if self.easy_deselect and not self.indexAt(e.position().toPoint()).isValid():
            self.selectionModel().clear()
        super().mousePressEvent(e)

    def set_hint(self, text, sub=""):
        self.hint, self.sub_hint = text, sub
        self.viewport().update()

    def _empty(self):
        return True

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self.hint or not self._empty():
            return
        p = QPainter(self.viewport())
        r = self.viewport().rect()
        f = p.font()
        f.setPointSizeF(f.pointSizeF() + 2.5)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(colour("muted"))
        p.drawText(r.adjusted(20, 0, -20, -22), Qt.AlignCenter | Qt.TextWordWrap, self.hint)
        if self.sub_hint:
            f.setPointSizeF(f.pointSizeF() - 2.5)
            f.setWeight(QFont.Normal)
            p.setFont(f)
            p.setPen(colour("faint"))
            p.drawText(r.adjusted(20, 34, -20, 0), Qt.AlignCenter | Qt.TextWordWrap, self.sub_hint)
        p.end()


class HintTable(_Hint, QTableWidget):
    def _empty(self):
        return self.rowCount() == 0


class HintTree(_Hint, QTreeWidget):
    def _empty(self):
        return self.topLevelItemCount() == 0


# ---------------------------------------------------------- per-item options
STAR = "★ "                     # marks a list row that has its own options
OWN_TIP = "Uses its own options"


def copy_opts(opts: dict) -> dict:
    return {k: list(v) if isinstance(v, list) else v for k, v in opts.items()}


def clean_opts(data, base: dict) -> dict:
    """Options read back from a save state: each key of `base` that has a value of the right
    type (track lists: indices) is taken from `data`, the rest stay as in `base`."""
    out = copy_opts(base)
    if not isinstance(data, dict):
        return out
    for k, v in base.items():
        got = data.get(k)
        if isinstance(v, list):
            if isinstance(got, list) and all(type(t) is int and t >= 0 for t in got):
                out[k] = sorted(set(got))
        elif type(got) is type(v):
            out[k] = got
    return out


class OptionBinding:
    """Links an options dict to the controls that show it. write() shows a dict without
    reporting anything; a change the user makes calls on_change(key, new value)."""

    def __init__(self, on_change):
        self.on_change = on_change
        self.fields = {}            # key -> (read, write)
        self.writing = False

    def add(self, key, read, write, signal):
        self.fields[key] = (read, write)
        signal.connect(lambda *_: self.writing or self.on_change(key, read()))

    def check(self, key, w):
        """A check box or a checkable group box."""
        self.add(key, w.isChecked, w.setChecked, w.toggled)

    def combo(self, key, w, by_text=False):
        if by_text:
            self.add(key, w.currentText, lambda v: w.setCurrentIndex(max(0, w.findText(v))), w.currentIndexChanged)
        else:
            self.add(key, w.currentData, lambda v: w.setCurrentIndex(max(0, w.findData(v))), w.currentIndexChanged)

    @contextmanager
    def quiet(self):
        """Change controls without it counting as the user's change."""
        old, self.writing = self.writing, True
        try:
            yield
        finally:
            self.writing = old

    def write(self, opts: dict):
        with self.quiet():
            for key, (_, put) in self.fields.items():
                if key in opts:
                    put(opts[key])


def card(layout_cls=QVBoxLayout, margins=(14, 12, 14, 12), spacing=8):
    """A raised rounded panel; returns (frame, layout)."""
    f = QFrame()
    f.setObjectName("card")
    lay = layout_cls(f)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return f, lay


class FullScreen(QWidget):
    """Shows a preview panel (video, timeline and controls) full screen and
    puts it back. The panel moves into this window, so the panel's own
    keyboard shortcuts are given again here as `keys` [(key, fn)]. F11, Esc
    or a double-click on `video` leave; F11 or a double-click also enter."""

    def __init__(self, panel: QWidget, video: QWidget, keys=(), title="Trimmer", parent=None):
        super().__init__(parent, Qt.Window)        # owned by `parent`, but its own window
        self.setWindowTitle(title)
        self.setObjectName("fullscreen")
        self.panel = panel
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(12, 12, 12, 12)
        self.home = None            # (layout, index, stretch) the panel came from
        for key, fn in list(keys) + [("F11", self.toggle), ("Esc", self.leave)]:
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(fn)
        self._watch = [video] + video.findChildren(QWidget)
        for w in self._watch:
            w.installEventFilter(self)

    @property
    def active(self) -> bool:
        return self.home is not None

    def eventFilter(self, obj, e):
        if e.type() == QEvent.MouseButtonDblClick and e.button() == Qt.LeftButton:
            self.toggle()
            return True
        return False

    def toggle(self):
        self.leave() if self.active else self.enter()

    def enter(self):
        parent = self.panel.parentWidget()
        layout = parent.layout() if parent is not None else None
        if self.active or layout is None:
            return
        i = layout.indexOf(self.panel)
        self.home = (layout, i, layout.stretch(i) if hasattr(layout, "stretch") else 0)
        self.lay.addWidget(self.panel)
        self.showFullScreen()
        self.activateWindow()

    def leave(self):
        if not self.active:
            return
        layout, i, stretch = self.home
        self.home = None
        layout.insertWidget(i, self.panel, stretch)
        self.hide()
        w = layout.parentWidget()
        if w is not None:
            w.window().activateWindow()

    def closeEvent(self, e):        # Alt+F4 while full screen: back to normal, keep the window
        self.leave()
        e.ignore()


class ClearButton(QToolButton):
    """Small x inside a line edit. Qt's built-in clear button sits off-centre
    under the stylesheet, so this one centres itself on every resize."""

    def __init__(self, line_edit):
        super().__init__(line_edit)
        self.le = line_edit
        self.setCursor(Qt.ArrowCursor)
        self.setToolTip("Clear")
        self.setObjectName("clear")
        self.setFocusPolicy(Qt.NoFocus)
        path = clear_icon_path()
        if path:
            self.setIcon(QIcon(path))
        else:
            self.setText("×")
        self.setIconSize(QSize(14, 14))
        self.setFixedSize(22, 22)
        self.clicked.connect(self._clear)
        line_edit.textChanged.connect(lambda t: self.setVisible(bool(t) and line_edit.isEnabled()))
        line_edit.setTextMargins(0, 0, 22, 0)
        line_edit.installEventFilter(self)
        self.setVisible(bool(line_edit.text()))

    def _clear(self):
        self.le.clear()
        self.le.textEdited.emit("")     # treat it like the user deleting the text
        self.le.setFocus()

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Resize, QEvent.Show):
            self.move(obj.width() - self.width() - 5, (obj.height() - self.height()) // 2)
        elif ev.type() == QEvent.EnabledChange:
            self.setVisible(bool(obj.text()) and obj.isEnabled())
        return False


def clearable(line_edit):
    """Give a line edit a centred, theme-matching clear (x) button."""
    line_edit._clear_button = ClearButton(line_edit)
    return line_edit


def label(text, kind=None, wrap=False):
    w = QLabel(text)
    if kind:
        w.setObjectName(kind)
    w.setWordWrap(wrap)
    return w


class Timeline(QWidget):
    """Clip timeline: kept segments, the pending in-point, the playhead.
    Click or drag to seek."""
    seek = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.duration = 0.0
        self.position = 0.0
        self.segments = []
        self.pending_in = None
        self.snap_marks = []          # keyframe positions where cuts will really start
        self.wave = []                # audio peaks 0..1, evenly spread over the clip
        self.thumbs = []              # (QImage) frames, evenly spread over the clip
        self.setMinimumHeight(72)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setToolTip("Click or drag to seek. I = mark in, O = mark out.")

    def set_state(self, duration=None, position=None, segments=None, pending_in="keep", snaps=None):
        if duration is not None:
            self.duration = duration
        if position is not None:
            self.position = position
        if segments is not None:
            self.segments = list(segments)
        if pending_in != "keep":
            self.pending_in = pending_in
        if snaps is not None:
            self.snap_marks = snaps
        self.update()

    def set_peek(self, waveform=None, thumbs=None):
        """Set the audio peaks and/or thumbnail JPEG bytes (None = leave, [] = clear)."""
        if waveform is not None:
            self.wave = [float(v) for v in waveform]
        if thumbs is not None:
            self.thumbs = [img for img in (QImage.fromData(b) for _, b in thumbs) if not img.isNull()]
        self.update()

    def _paint_peek(self, p, bar):
        """Thumbnail strip, then the waveform along the bottom, both muted."""
        clip = QPainterPath()
        clip.addRoundedRect(bar, 6, 6)
        p.save()
        p.setClipPath(clip)
        if self.thumbs:
            n = len(self.thumbs)
            slot = bar.width() / n
            for i, img in enumerate(self.thumbs):
                dst = QRectF(bar.left() + i * slot, bar.top(), slot + 1, bar.height())
                sw = min(img.width(), img.height() * dst.width() / dst.height())
                src = QRectF((img.width() - sw) / 2, 0, sw, img.height())
                p.drawImage(dst, img, src)
            veil = QColor(colour("surface2"))
            veil.setAlpha(140)
            p.fillRect(bar, veil)
        if self.wave:
            n = len(self.wave)
            col = QColor(colour("text"))
            col.setAlpha(150)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            bw = bar.width() / n
            top = bar.height() * 0.55
            for i, v in enumerate(self.wave):
                bh = max(1.0, top * v ** 0.6)
                p.drawRect(QRectF(bar.left() + i * bw, bar.bottom() - bh, max(bw, 1.0), bh))
        p.restore()

    def _x(self, t, w):
        return 8 + (w - 16) * (t / self.duration if self.duration else 0)

    def _t(self, x):
        w = self.width()
        return max(0.0, min(self.duration, (x - 8) / max(1, w - 16) * self.duration))

    def mousePressEvent(self, e):
        if self.duration:
            self.seek.emit(self._t(e.position().x()))

    def mouseMoveEvent(self, e):
        if self.duration and e.buttons() & Qt.LeftButton:
            self.seek.emit(self._t(e.position().x()))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        bar = QRectF(8, 12, w - 16, h - 30)
        p.setPen(Qt.NoPen)
        p.setBrush(colour("surface2"))
        p.drawRoundedRect(bar, 6, 6)
        if self.duration:
            if self.thumbs or self.wave:
                self._paint_peek(p, bar)
            if self.segments:
                acc = colour("accent")
                fill = QColor(acc)
                fill.setAlpha(95)
                if self.thumbs or self.wave:
                    # Over pictures a tint is hard to see: dim what is cut away
                    # instead, and keep a light tint on what is kept.
                    fill.setAlpha(40)
                    shade = QColor(colour("surface2"))
                    shade.setAlpha(200)
                    cut = QPainterPath()
                    cut.addRoundedRect(bar, 6, 6)
                    for s, e in self.segments:
                        e = self.duration if e is None else e
                        keep = QPainterPath()
                        keep.addRect(QRectF(self._x(s, w), bar.top(), self._x(e, w) - self._x(s, w), bar.height()))
                        cut = cut.subtracted(keep)
                    p.fillPath(cut, shade)
                for s, e in self.segments:
                    e = self.duration if e is None else e
                    seg = QRectF(self._x(s, w), bar.top(), self._x(e, w) - self._x(s, w), bar.height())
                    p.setPen(Qt.NoPen)
                    p.setBrush(fill)
                    p.drawRoundedRect(seg, 4, 4)
                    p.setPen(QPen(acc, 2))
                    p.drawLine(seg.topLeft(), seg.bottomLeft())
                    p.drawLine(seg.topRight(), seg.bottomRight())
            if self.pending_in is not None:
                p.setPen(QPen(QColor("#f5c451"), 2))
                x = self._x(self.pending_in, w)
                p.drawLine(int(x), int(bar.top()) - 3, int(x), int(bar.bottom()) + 3)
            p.setPen(QPen(QColor(245, 150, 90, 200), 1, Qt.DashLine))
            for k in self.snap_marks:
                x = self._x(k, w)
                p.drawLine(int(x), int(bar.top()), int(x), int(bar.bottom()))
            x = self._x(self.position, w)
            p.setPen(QPen(colour("text"), 2))
            p.drawLine(QPointF(x, bar.top() - 4), QPointF(x, bar.bottom() + 4))
            p.setPen(Qt.NoPen)
            p.setBrush(colour("text"))
            p.drawEllipse(QPointF(x, bar.top() - 5), 4.5, 4.5)
            p.setPen(colour("faint"))
            f = p.font()
            f.setPointSizeF(max(7.5, f.pointSizeF() - 1.5))
            p.setFont(f)
            p.drawText(QRectF(8, h - 13, 120, 13), Qt.AlignLeft, "0:00")
            p.drawText(QRectF(w - 128, h - 13, 120, 13), Qt.AlignRight, format_hms_ms(self.duration))
        p.end()


MODES = [("Quality (CRF)", "crf"), ("Auto quality, fast (auto2)", "auto2"),
         ("Auto quality, measured (auto3)", "auto3"), ("Auto settings, measured (codec and CRF)", "autoset"),
         ("Target file size", "target_size"),
         ("Lossless", "lossless")]
AUDIO = [("Copy original (no loss)", "copy"), ("AAC 96 kbps", "96"), ("AAC 128 kbps", "128"),
         ("AAC 160 kbps", "160"), ("AAC 192 kbps", "192"), ("AAC 256 kbps", "256"), ("AAC 320 kbps", "320")]
HEIGHTS = [("Keep", 0), ("2160p", 2160), ("1440p", 1440), ("1080p", 1080), ("720p", 720), ("480p", 480)]
FPS = [("Keep", 0), ("60 fps", 60), ("30 fps", 30)]
AUTO3_TARGETS = [("95: very hard to tell apart (bigger)", 95.0), ("93: balanced (default)", 93.0),
                 ("90: smaller, small visible loss", 90.0)]


class EncodeForm(QWidget):
    """Edit an EncodeSettings. `encoders` = encoder keys that work here."""
    changed = Signal()

    def __init__(self, encoders=None, parent=None):
        super().__init__(parent)
        f = QFormLayout(self)
        f.setContentsMargins(0, 0, 0, 0)
        f.setHorizontalSpacing(14)          # labels like "Keyframe every" don't touch their box
        self.mode = QComboBox()
        for label, key in MODES:
            self.mode.addItem(label, key)
        self.codec = QComboBox()
        for key, label in CODECS.items():
            self.codec.addItem({"h264": "H.264 (plays everywhere)", "h265": "H.265 / HEVC (smaller)",
                                "av1": "AV1 (smallest, slow)"}[key], key)
        self.encoder = QComboBox()
        self.set_encoders(encoders or ["software"])
        self.crf = QSpinBox()
        self.crf.setRange(0, 63)
        self.crf.setValue(23)
        self.crf.setToolTip("Lower = better quality and bigger files. 18-28 is the useful range.")
        self.preset = QComboBox()
        self.preset.addItems(VALID_PRESETS)
        self.preset.setToolTip("Slower presets make smaller files at the same quality.")
        self.target = QDoubleSpinBox()
        self.target.setRange(0.5, 100000)
        self.target.setSuffix(" MB")
        self.target.setDecimals(1)
        self.audio = QComboBox()
        for label, key in AUDIO:
            self.audio.addItem(label, key)
        self.height_cap = QComboBox()
        for label, v in HEIGHTS:
            self.height_cap.addItem(label, v)
        self.fps_cap = QComboBox()
        for label, v in FPS:
            self.fps_cap.addItem(label, v)
        self.gop = QDoubleSpinBox()
        self.gop.setRange(0, 20)
        self.gop.setSingleStep(0.5)
        self.gop.setSuffix(" s")
        self.gop.setSpecialValueText("Encoder default")
        self.ten_bit = QCheckBox("10-bit colour (about 5-10% smaller, less banding)")
        self.ten_bit.setToolTip(
            "Same quality in a smaller file, and smoother skies and dark scenes.\n"
            "H.265 and AV1 only. 10-bit H.265 may not preview in Discord or some browsers,\n"
            "and very old devices may not play it. Leave off for clips you share.\n"
            "It can't be undone later without re-encoding.")
        self.revert = QCheckBox("Keep the original if compressing would make it bigger")
        self.revert_hint = QLabel()
        f.addRow("Method", self.mode)
        f.addRow("Codec", self.codec)
        f.addRow("Encoder", self.encoder)
        self.crf_row = f.rowCount()
        f.addRow("CRF / quality", self.crf)
        self.auto3_target = QComboBox()
        for label, v in AUTO3_TARGETS:
            self.auto3_target.addItem(label, v)
        self.auto3_target.setCurrentIndex(1)
        self.auto3_target.setToolTip("How close to the original auto3 keeps each clip, as a VMAF score out of 100.\n"
                                     "It picks the smallest file that still scores at least this.")
        f.addRow("Quality target", self.auto3_target)
        self.give_up = QSlider(Qt.Horizontal)
        self.give_up.setRange(0, 100)
        self.give_up.setValue(32)
        self.give_up.setToolTip("How much quality you're willing to give up for a smaller file.\n"
                                "0 keeps it as close to the original as possible (VMAF 99), 32 is balanced\n"
                                "(VMAF 93), 100 is the smallest files with clearly visible loss (VMAF 80).\n"
                                "Each clip is tested with H.265, AV1 and H.264 at several CRFs, and the\n"
                                "smallest one that keeps this quality is used. The queue analyses clips\n"
                                "before it can start.")
        self.give_up_text = QLabel()
        self.give_up_text.setMinimumWidth(150)
        self.give_up_row = hbox(self.give_up, self.give_up_text, stretch_end=False)
        f.addRow("Quality to give up", self.give_up_row)
        f.addRow("Target size", self.target)
        f.addRow("Speed preset", self.preset)
        f.addRow("Audio", self.audio)
        f.addRow("Max resolution", self.height_cap)
        f.addRow("Max frame rate", self.fps_cap)
        f.addRow("Keyframe every", self.gop)
        f.addRow("", self.ten_bit)
        f.addRow("", self.revert)
        self.form = f
        for w in (self.mode, self.codec, self.encoder, self.preset, self.audio, self.height_cap, self.fps_cap,
                  self.auto3_target):
            w.currentIndexChanged.connect(self._update)
        for w in (self.crf, self.target, self.gop, self.give_up):
            w.valueChanged.connect(self._update)
        self.revert.toggled.connect(self._update)
        self.ten_bit.toggled.connect(self._update)
        self._update()

    def set_encoders(self, encoders):
        cur = self.encoder.currentData()
        self.encoder.blockSignals(True)
        self.encoder.clear()
        for k in ENCODERS:
            if k in encoders:
                self.encoder.addItem(ENCODERS[k], k)
        i = self.encoder.findData(cur)
        self.encoder.setCurrentIndex(max(0, i))
        self.encoder.blockSignals(False)

    def _update(self):
        mode = self.mode.currentData()
        self.form.setRowVisible(self.crf, mode == "crf")
        self.form.setRowVisible(self.auto3_target, mode == "auto3")
        self.form.setRowVisible(self.give_up_row, mode == "autoset")
        self.give_up_text.setText(f"{self.give_up.value()}  (VMAF {give_up_target(self.give_up.value()):g})")
        self.codec.setEnabled(mode != "autoset")
        self.codec.setToolTip("Auto settings tries H.265, AV1 and H.264 and picks per clip"
                              if mode == "autoset" else "")
        self.form.setRowVisible(self.target, mode == "target_size")
        self.form.setRowVisible(self.revert, mode != "target_size")
        self.encoder.setEnabled(mode != "lossless")
        self.ten_bit.setEnabled(mode == "autoset" or (mode != "lossless" and self.codec.currentData() in ("h265", "av1")))
        self.changed.emit()

    def settings(self) -> EncodeSettings:
        mode = self.mode.currentData()
        s = EncodeSettings(
            mode="lossy" if mode in ("crf", "auto2", "auto3", "autoset") else mode,
            codec=self.codec.currentData(), encoder=self.encoder.currentData() or "software",
            quality=mode if mode in ("auto2", "auto3", "autoset") else self.crf.value(),
            preset=self.preset.currentText(), target_size_mb=self.target.value(),
            audio=self.audio.currentData(), max_height=self.height_cap.currentData(),
            max_fps=self.fps_cap.currentData(), keyframe_interval=self.gop.value(),
            auto3_target=self.auto3_target.currentData(), revert_if_larger=self.revert.isChecked(),
            ten_bit=self.ten_bit.isChecked(), give_up=self.give_up.value())
        return s.validate()

    def set_settings(self, s: EncodeSettings):
        key = s.mode if s.mode != "lossy" else (s.quality if s.quality in ("auto2", "auto3", "autoset") else "crf")
        self.mode.setCurrentIndex(max(0, self.mode.findData(key)))
        self.codec.setCurrentIndex(max(0, self.codec.findData(s.codec)))
        self.encoder.setCurrentIndex(max(0, self.encoder.findData(s.encoder)))
        if isinstance(s.quality, int):
            self.crf.setValue(s.quality)
        self.preset.setCurrentText(s.preset)
        self.target.setValue(s.target_size_mb)
        self.audio.setCurrentIndex(max(0, self.audio.findData(s.audio)))
        self.height_cap.setCurrentIndex(max(0, self.height_cap.findData(s.max_height)))
        self.fps_cap.setCurrentIndex(max(0, self.fps_cap.findData(int(s.max_fps))))
        self.gop.setValue(s.keyframe_interval)
        # Nearest offered target (a hand-edited settings file may hold another number).
        near = min(range(len(AUTO3_TARGETS)), key=lambda i: abs(AUTO3_TARGETS[i][1] - s.auto3_target))
        self.auto3_target.setCurrentIndex(near)
        self.revert.setChecked(s.revert_if_larger)
        self.ten_bit.setChecked(s.ten_bit)
        self.give_up.setValue(s.give_up)
        self._update()


def hbox(*widgets, stretch_end=True):
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for x in widgets:
        if x == "stretch":
            lay.addStretch(1)
        else:
            lay.addWidget(x)
    if stretch_end:
        lay.addStretch(1)
    return w


class TrackPicker(QComboBox):
    """Chooses which audio track the preview plays. Right-click a track (in
    the list or on the box) to make it the default for every clip opened
    after it, until the app closes. `prefs` is a dict shared app-wide."""
    track_chosen = Signal(int)

    def __init__(self, prefs: dict, parent=None):
        super().__init__(parent)
        self.prefs = prefs
        self.labels, self.titles = [], []
        self.setToolTip("Which audio track to hear. Right-click a track to always start with it.")
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.view().viewport().installEventFilter(self)
        self.currentIndexChanged.connect(lambda i: self.track_chosen.emit(i) if i >= 0 else None)

    def set_tracks(self, labels, titles=None):
        """Fill with the clip's tracks and select the preferred one."""
        self.labels = list(labels)
        self.titles = list(titles or labels)
        self.blockSignals(True)
        self.clear()
        for i, t in enumerate(self.labels):
            self.addItem(t)
        self._mark()
        self.setCurrentIndex(self.preferred())
        self.blockSignals(False)
        if self.count():
            self.track_chosen.emit(self.currentIndex())

    def preferred(self) -> int:
        """The default track: same name if this clip has it, else same position.
        When several tracks share that name, the one at the same position wins."""
        if not self.labels:
            return -1
        title, index = self.prefs.get("title"), self.prefs.get("index")
        same = [i for i, t in enumerate(self.titles) if title and t == title]
        if same:
            return index if index in same else same[0]
        if index is not None and 0 <= index < len(self.labels):
            return index
        return 0

    def set_default(self, i):
        if 0 <= i < len(self.labels):
            self.prefs["title"], self.prefs["index"] = self.titles[i], i
            self._mark()
            self.setCurrentIndex(i)

    def clear_default(self):
        self.prefs.pop("title", None)
        self.prefs.pop("index", None)
        self._mark()

    def _mark(self):
        default = self.preferred() if ("title" in self.prefs or "index" in self.prefs) else -1
        for i, t in enumerate(self.labels):
            self.setItemText(i, t + ("   ★ default" if i == default else ""))

    def _menu(self, pos):
        if not self.labels:
            return
        m = QMenu(self)
        for i, t in enumerate(self.labels):
            m.addAction(f"Always start with {t}", lambda i=i: self.set_default(i))
        if "title" in self.prefs or "index" in self.prefs:
            m.addSeparator()
            m.addAction("Clear the default track", self.clear_default)
        m.exec(self.mapToGlobal(pos))

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.MouseButtonRelease and ev.button() == Qt.RightButton:
            idx = self.view().indexAt(ev.position().toPoint())
            if idx.isValid():
                self.hidePopup()
                self.set_default(idx.row())
                return True
        if ev.type() == QEvent.MouseButtonPress and ev.button() == Qt.RightButton:
            return True     # don't let a right-click pick the item as a normal click
        return super().eventFilter(obj, ev)
