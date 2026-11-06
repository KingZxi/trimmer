"""Settings dialog and the OBS tips page."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFrame,
                               QFileDialog, QFormLayout, QGroupBox, QInputDialog, QKeySequenceEdit, QLabel, QLineEdit,
                               QListWidget, QSpinBox,
                               QMessageBox, QPushButton, QScrollArea, QSizePolicy, QTabWidget, QTextBrowser, QToolButton,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, AUTHOR, AUTHOR_URL, COPYRIGHT, LICENSE_NAME, __version__
from ..core import ffmpeg as ff
from ..core import savings
from ..core.encode import EncodeSettings
from ..core.render import AUDIO_RIP_FORMATS
from ..core.settings import AppSettings, default_presets
from ..core.journal import clean_logs, log_files
from ..core.util import app_data_dir
from .system_hooks import DEFAULT_IGNORE, IS_WINDOWS, play_chime
from .theme import set_kind
from .widgets import EncodeForm, hbox, label


def _value(w):
    """A settings control's current value (comparable between two dialogs)."""
    if isinstance(w, (QCheckBox, QGroupBox)):
        return w.isChecked()
    if isinstance(w, QComboBox):
        return w.currentIndex()
    if isinstance(w, (QSpinBox, QDoubleSpinBox)):
        return round(w.value(), 4)
    if isinstance(w, QLineEdit):
        return w.text().strip()
    if isinstance(w, QKeySequenceEdit):
        return w.keySequence().toString(QKeySequence.PortableText)
    return None


def _set_value(w, v):
    if isinstance(w, (QCheckBox, QGroupBox)):
        w.setChecked(v)
    elif isinstance(w, QComboBox):
        w.setCurrentIndex(v)
    elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
        w.setValue(v)
    elif isinstance(w, QLineEdit):
        w.setText(v)
    elif isinstance(w, QKeySequenceEdit):
        w.setKeySequence(QKeySequence(v))


def _changed_signal(w):
    if isinstance(w, (QCheckBox, QGroupBox)):
        return w.toggled
    if isinstance(w, QComboBox):
        return w.currentIndexChanged
    if isinstance(w, (QSpinBox, QDoubleSpinBox)):
        return w.valueChanged
    if isinstance(w, QLineEdit):
        return w.textChanged
    return w.keySequenceChanged


def _shown(w, v) -> str:
    """How a default value reads to a person, for the reset button's tooltip."""
    if isinstance(w, (QCheckBox, QGroupBox)):
        return "on" if v else "off"
    if isinstance(w, QComboBox):
        return w.itemText(v)
    if isinstance(w, (QSpinBox, QDoubleSpinBox)):
        return f"{w.prefix()}{w.textFromValue(v)}{w.suffix()}"
    return f'"{v}"' if v else "empty"


CONTROLS = (QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QKeySequenceEdit)


class SettingsDialog(QDialog):
    def __init__(self, settings, encoders, parent=None, _defaults=False):
        super().__init__(parent)
        self.s = settings
        self.encoders = encoders
        self.setWindowTitle("Settings")
        self.resize(620, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 16)
        lay.setSpacing(12)
        tabs = QTabWidget()
        self.tabs = tabs
        tabs.setDocumentMode(True)
        lay.addWidget(tabs)
        pages = [(self._general(), "General"), (self._compression(), "Compression"),
                 (self._merge(), "Replay merge"), (self._queue(), "Queue"), (self._about(), "About")]
        if not _defaults:
            self._add_resets([page for page, _ in pages])
        for page, title in pages:
            # Each page scrolls, so the dialog (and its Save button) fits on small screens.
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.NoFrame)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            area.setWidget(page)
            area.setMinimumWidth(page.sizeHint().width() + area.verticalScrollBar().sizeHint().width())
            tabs.addTab(area, title)
        tallest = max(page.sizeHint().height() for page, _ in pages)
        room = self.screen().availableGeometry().height() - 60 if self.screen() else 900
        # Every tab must show. Measure them in the bold font the theme draws them in (Qt sizes tabs with
        # the plain font otherwise), and widen the dialog instead of scrolling the first tabs away.
        bar = tabs.tabBar()
        bold = bar.font()
        bold.setWeight(QFont.DemiBold)
        bar.setFont(bold)
        bar.setUsesScrollButtons(False)
        bar.setElideMode(Qt.ElideNone)
        self.setMinimumWidth(bar.sizeHint().width() + 16 + 36 + 24)
        self.resize(max(620, self.width(), self.minimumWidth()), max(420, min(tallest + 130, room)))
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setProperty("kind", "primary")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    # --------------------------------------------------------------- queue
    def _queue(self):
        s = self.s
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(4, 14, 4, 4)
        f.setVerticalSpacing(10)
        f.setHorizontalSpacing(18)
        self.game_pause = QCheckBox("Pause the queue while a game is full screen (or borderless), "
                                    "and carry on when you leave it")
        self.game_pause.setChecked(s.game_pause)
        self.game_pause.setEnabled(IS_WINDOWS)
        f.addRow("", self.game_pause)
        self.game_ignore = QLineEdit(s.game_pause_ignore or DEFAULT_IGNORE)
        self.game_ignore.setToolTip("Programs that never pause the queue when full screen, separated by commas "
                                    "(e.g. browsers and video players). Clear it to get the built-in list back.")
        self.game_ignore.setEnabled(IS_WINDOWS)
        f.addRow("Never pause for", self.game_ignore)
        self.hotkey_edit = QKeySequenceEdit(QKeySequence(s.pause_hotkey))
        self.hotkey_edit.setMaximumSequenceLength(1)
        self.hotkey_edit.setToolTip("Click, then press the keys. Works in every program, even full screen games.")
        self.hotkey_edit.setEnabled(IS_WINDOWS)
        b_none = QPushButton("None")
        b_none.setToolTip("No global key")
        b_none.clicked.connect(self.hotkey_edit.clear)
        f.addRow("Global key to pause / resume", hbox(self.hotkey_edit, b_none, stretch_end=False))
        self.sounds = QCheckBox("Play a sound when the global key or a game pauses or resumes the queue")
        self.sounds.setChecked(s.pause_sounds)
        b_hear = QPushButton("Hear them")
        b_hear.clicked.connect(lambda: (play_chime("paused"), QTimer.singleShot(450, lambda: play_chime("resumed"))))
        f.addRow("", hbox(self.sounds, b_hear, stretch_end=False))
        if not IS_WINDOWS:
            f.addRow("", label("Game detection and the global key work on Windows only.", "muted"))
        return w

    # --------------------------------------------------------------- about
    def _about(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 18, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(label(f"{APP_NAME} {__version__}", "title"))
        lay.addWidget(label("Trim, merge, compress and rip OBS clips.", "muted"))
        lay.addSpacing(10)
        made = label(f'Created by <b>{AUTHOR}</b>')
        lay.addWidget(made)
        link = label(f'<a href="{AUTHOR_URL}">GitHub: {AUTHOR}</a>')
        link.setOpenExternalLinks(True)          # opens in your browser; Trimmer itself stays offline
        lay.addWidget(link)
        lay.addWidget(label(COPYRIGHT, "muted"))
        lay.addWidget(label(f"Free software under the {LICENSE_NAME} licence, with no warranty. You may share "
                            "and change it, as long as what you share stays under the same licence.",
                            "muted", wrap=True))
        lay.addSpacing(10)
        lay.addWidget(label("Video work is done by ffmpeg, and the window by Qt (PySide6).", "muted", wrap=True))
        lay.addStretch(1)
        return w

    # ------------------------------------------------------------- general
    def _general(self):
        s = self.s
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(4, 14, 4, 4)
        f.setVerticalSpacing(10)
        f.setHorizontalSpacing(18)
        self.out = QLineEdit(s.output_dir)
        self.out.setPlaceholderText("Same folder as each original")
        b = QPushButton("Browse…")
        b.clicked.connect(lambda: self._pick(self.out))
        f.addRow("Output folder", hbox(self.out, b, stretch_end=False))
        self.conflict = QComboBox()
        for text, key in (("Ask me", "ask"), ("Keep both (rename the new file)", "rename"),
                          ("Overwrite", "overwrite"), ("Skip", "skip")):
            self.conflict.addItem(text, key)
        self.conflict.setCurrentIndex(self.conflict.findData(s.conflict_policy))
        f.addRow("If a file already exists", self.conflict)
        self.delete = QCheckBox("Tick 'Delete originals' by default")
        self.delete.setChecked(s.delete_originals)
        f.addRow("", self.delete)
        self.permanent = QComboBox()
        self.permanent.addItem("Move to the Recycle Bin (recoverable)", False)
        self.permanent.addItem("Delete permanently", True)
        self.permanent.setCurrentIndex(1 if s.permanent_delete else 0)
        f.addRow("Deleting originals", self.permanent)
        self.count_kept = QCheckBox("Count space saved even when originals are kept (I delete them myself later)")
        self.count_kept.setChecked(s.count_kept_savings)
        f.addRow("", self.count_kept)
        self.savings_info = label(savings.summary_text(), "muted")
        b_edit = QPushButton("Edit…")
        b_edit.setToolTip("Set the space saved total yourself")
        b_edit.clicked.connect(lambda: self._edit_savings())
        b_reset = QPushButton("Reset")
        b_reset.clicked.connect(lambda: self._reset_savings())
        f.addRow("Space saved", hbox(self.savings_info, "stretch", b_edit, b_reset, stretch_end=False))
        self.suffix = QLineEdit(s.name_suffix)
        self.suffix.setPlaceholderText("e.g. _edit")
        f.addRow("Add to output names", self.suffix)
        self.tags = QCheckBox("Add what was done to names, e.g. (trimmed), (no audio)")
        self.tags.setChecked(s.add_operation_tags)
        f.addRow("", self.tags)
        self.trim_range = QCheckBox("Include the kept times, e.g. (trimmed 1m05s-1m32s)")
        self.trim_range.setChecked(s.add_trim_range)
        self.trim_range.setEnabled(s.add_operation_tags)
        self.tags.toggled.connect(self.trim_range.setEnabled)
        f.addRow("", self.trim_range)
        self.prefill = QCheckBox("When renaming, start with the original name so you can add to it")
        self.prefill.setChecked(s.prefill_names)
        f.addRow("", self.prefill)
        self.qlabel = QCheckBox("Add compression details to compressed file names, e.g. (LOSSY), (CQ28, H265 NVENC)")
        self.qlabel.setChecked(s.add_quality_label)
        f.addRow("", self.qlabel)
        self.warn_dup = QCheckBox("Warn when a job is already in the queue or finished")
        self.warn_dup.setChecked(s.warn_duplicate_jobs)
        f.addRow("", self.warn_dup)
        self.warn_done = QCheckBox("Warn when a file looks already trimmed, compressed or merged by Trimmer")
        self.warn_done.setChecked(s.warn_already_processed)
        f.addRow("", self.warn_done)
        self.container = QComboBox()
        for text, key in (("Same as the original (MP4/MKV/MOV, others become MP4)", "same"),
                           ("Always MP4", ".mp4"), ("Always MKV", ".mkv")):
            self.container.addItem(text, key)
        self.container.setCurrentIndex(max(0, self.container.findData(s.container)))
        f.addRow("File type", self.container)
        self.exact = QCheckBox("Frame-exact trims by default (instead of starting on the keyframe before)")
        self.exact.setChecked(s.exact_cuts)
        f.addRow("", self.exact)
        self.smart = QCheckBox("Smart cut: re-encode only the second or two at each cut, copy the rest")
        self.smart.setToolTip("Frame-exact trims keep the original quality and save almost instantly.\n"
                              "Works for H.264 and H.265 clips. Every join is checked against the\n"
                              "original; if it doesn't match, the whole trim is re-encoded instead.")
        self.smart.setChecked(s.smart_cut)
        f.addRow("", self.smart)
        self.peek = QCheckBox("Show waveform and thumbnails on the timeline")
        self.peek.setChecked(s.show_peek)
        f.addRow("", self.peek)
        self.rip = QComboBox()
        for k, text in AUDIO_RIP_FORMATS.items():
            self.rip.addItem(text, k)
        self.rip.setCurrentIndex(max(0, self.rip.findData(s.rip_format)))
        f.addRow("Ripped audio format", self.rip)
        self.ffdir = QLineEdit(s.ffmpeg_dir)
        self.ffdir.setPlaceholderText("Use ffmpeg from PATH")
        b2 = QPushButton("Browse…")
        b2.clicked.connect(lambda: self._pick(self.ffdir))
        f.addRow("ffmpeg folder", hbox(self.ffdir, b2, stretch_end=False))
        ver = QLabel(ff.ffmpeg_version())
        ver.setWordWrap(True)
        f.addRow("ffmpeg found", ver)
        hw = [k for k in self.encoders if k != "software"]
        f.addRow("GPU encoders", QLabel(", ".join(hw) if hw else "none detected (software encoding only)"))
        self.theme = QComboBox()
        self.theme.addItem("System (match Windows)", "system")
        self.theme.addItem("Dark", "dark")
        self.theme.addItem("Light", "light")
        self.theme.setCurrentIndex(max(0, self.theme.findData(s.theme)))
        f.addRow("Theme", self.theme)
        self.on_close = QComboBox()
        for text, key in (("Ask me", "ask"), ("Save it and restore it next time", "save"),
                          ("Close without saving", "discard")):
            self.on_close.addItem(text, key)
        self.on_close.setCurrentIndex(max(0, self.on_close.findData(s.on_close)))
        f.addRow("On close", self.on_close)
        f.addRow("Settings and logs", QLabel(str(app_data_dir())))
        self.auto_logs = QCheckBox("Delete logs and crash reports older than")
        self.auto_logs.setChecked(s.auto_delete_logs)
        self.auto_logs.setToolTip("Checked when Trimmer starts. Untick to keep every log.")
        self.keep_logs = QSpinBox()
        self.keep_logs.setRange(1, 3650)
        self.keep_logs.setSuffix(" days")
        self.keep_logs.setValue(min(3650, max(1, int(s.keep_logs_days))))
        self.keep_logs.setEnabled(s.auto_delete_logs)
        self.auto_logs.toggled.connect(self.keep_logs.setEnabled)
        self.b_clear_logs = QPushButton("Delete all logs now")
        self.b_clear_logs.clicked.connect(self._clear_logs)
        f.addRow("Logs", hbox(self.auto_logs, self.keep_logs, self.b_clear_logs, stretch_end=False))
        return w

    # ------------------------------------------------- restore defaults
    def _controls(self):
        return {k: v for k, v in vars(self).items() if isinstance(v, CONTROLS)
                or (isinstance(v, QGroupBox) and v.isCheckable())}

    def _add_resets(self, pages):
        """A small ↺ button at the end of every row whose setting isn't the default; it puts that
        row back to the default (nothing is saved until Save)."""
        ref = SettingsDialog(AppSettings(), self.encoders, _defaults=True)
        defaults = {k: _value(w) for k, w in ref._controls().items()}
        shown = {k: _shown(w, defaults[k]) for k, w in ref._controls().items()}
        self._default_enc = ref.enc.settings().to_dict()
        ref.deleteLater()
        controls = {w: k for k, w in self._controls().items() if k in defaults}
        self.resets = {}
        for form in [f for page in pages for f in page.findChildren(QFormLayout)]:
            for row in range(form.rowCount()):
                item = form.itemAt(row, QFormLayout.FieldRole)
                field = item.widget() if item is not None else None
                if field is None:
                    continue
                inside = [w for w in [field] + field.findChildren(QWidget) if w in controls]
                if not inside:
                    continue
                keys = [controls[w] for w in inside]
                b = QToolButton()
                b.setText("↺")
                b.setAutoRaise(True)
                b.setFixedSize(28, 28)
                b.setStyleSheet("QToolButton { padding: 0; font-size: 12pt; }")
                b.setCursor(Qt.PointingHandCursor)
                b.setToolTip("Restore the default: " + ", ".join(shown[k] for k in keys))
                policy = b.sizePolicy()
                policy.setRetainSizeWhenHidden(True)      # rows don't shift as it comes and goes
                b.setSizePolicy(policy)
                form.removeWidget(field)
                form.setWidget(row, QFormLayout.FieldRole, hbox(field, b, stretch_end=False))
                field.setSizePolicy(QSizePolicy.Expanding, field.sizePolicy().verticalPolicy())

                def refresh(_=None, b=b, keys=keys):
                    b.setVisible(any(_value(getattr(self, k)) != defaults[k] for k in keys))

                def restore(_=False, keys=keys, refresh=refresh):
                    for k in keys:
                        _set_value(getattr(self, k), defaults[k])
                    refresh()
                b.clicked.connect(restore)
                for k in keys:
                    _changed_signal(getattr(self, k)).connect(refresh)
                refresh()
                for k in keys:
                    self.resets[k] = b
        # The custom compression settings: one button for the whole form.
        self.b_enc_default.clicked.connect(lambda: (self.enc.set_settings(EncodeSettings(**self._default_enc)),
                                                    self._enc_refresh()))
        for w in self.enc.findChildren(QWidget):   # the form's own controls
            if isinstance(w, CONTROLS):
                _changed_signal(w).connect(self._enc_refresh)
        self._enc_refresh()

    def _enc_refresh(self, *_):
        if hasattr(self, "_default_enc"):
            self.b_enc_default.setVisible(self.enc.settings().to_dict() != self._default_enc)

    def show_tab(self, title: str):
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == title:
                self.tabs.setCurrentIndex(i)

    def _clear_logs(self):
        n = len(log_files())
        if not n:
            QMessageBox.information(self, "Logs", "There are no logs to delete.")
            return
        if QMessageBox.question(self, "Delete logs",
                                f"Delete {n} run log(s) and crash report(s)? Settings, the queue and "
                                "the space saved total are kept.") == QMessageBox.Yes:
            gone = clean_logs(0)
            QMessageBox.information(self, "Logs", f"Deleted {gone} file(s).")

    def _pick(self, line):
        d = QFileDialog.getExistingDirectory(self, "Choose folder", line.text())
        if d:
            line.setText(d)

    # --------------------------------------------------------- compression
    def _compression(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.b_enc_default = set_kind(QPushButton("↺ Restore default"), "ghost")
        self.b_enc_default.setToolTip("Put the custom compression settings back to the defaults (H.265, CRF 23)")
        self.b_enc_default.hide()
        lay.addWidget(hbox(QLabel("Custom settings (used when 'Custom (Settings)' is picked):"), "stretch",
                           self.b_enc_default, stretch_end=False))
        self.enc = EncodeForm(self.encoders)
        self.enc.set_settings(self.s.encode_settings())
        lay.addWidget(self.enc)
        lay.addWidget(QLabel("Saved presets:"))
        self.presets = dict(self.s.presets)
        self.plist = QListWidget()
        self.plist.addItems(list(self.presets.keys()))
        self.plist.setMaximumHeight(110)
        lay.addWidget(self.plist)
        b_save = QPushButton("Save custom settings as preset…")
        b_save.clicked.connect(self._save_preset)
        b_load = QPushButton("Load preset into custom")
        b_load.clicked.connect(self._load_preset)
        b_del = QPushButton("Delete preset")
        b_del.clicked.connect(self._del_preset)
        b_reset = QPushButton("Restore defaults")
        b_reset.clicked.connect(self._reset_presets)
        lay.addWidget(hbox(b_save, b_load, b_del, b_reset))
        return w

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, "Save preset", "Preset name:")
        if ok and name.strip():
            self.presets[name.strip()] = self.enc.settings().to_dict()
            self._refresh_presets()

    def _load_preset(self):
        it = self.plist.currentItem()
        if it:
            self.enc.set_settings(EncodeSettings.from_dict(self.presets[it.text()]))

    def _del_preset(self):
        it = self.plist.currentItem()
        if it:
            self.presets.pop(it.text(), None)
            self._refresh_presets()

    def _reset_presets(self):
        self.presets = default_presets()
        self._refresh_presets()

    def _refresh_presets(self):
        self.plist.clear()
        self.plist.addItems(list(self.presets.keys()))

    # --------------------------------------------------------------- merge
    def _merge(self):
        s = self.s
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(4, 14, 4, 4)
        f.setVerticalSpacing(10)
        f.setHorizontalSpacing(18)
        self.show_merge = QCheckBox("Show the Replay merge tab")
        self.show_merge.setChecked(s.show_replay_merge)
        self.show_merge.setToolTip("Untick if you only trim clips. Hiding it also stops watching the replay folder; "
                                   "nothing else changes, and ticking it brings everything back.")
        f.addRow("", self.show_merge)
        self.packets = QCheckBox("Match clips frame by frame (exact, lossless) when they share footage")
        self.packets.setChecked(bool(s.ui.get("packet_matching", True)))
        f.addRow("", self.packets)
        self.gap = QDoubleSpinBox()
        self.gap.setRange(0, 600)
        self.gap.setSuffix(" s")
        self.gap.setValue(s.max_gap)
        self.gap.setToolTip("A clip that starts more than this after the previous one ends starts a new session.")
        f.addRow("Max gap within a session", self.gap)
        fb = QGroupBox("Only used when clips can't be matched exactly")
        fbl = QVBoxLayout(fb)
        fbl.addWidget(label("Clips from the same OBS session are normally joined frame by frame, "
                            "so these rarely apply.", "muted", wrap=True))
        gf = QFormLayout()
        gf.setVerticalSpacing(10)
        gf.setHorizontalSpacing(18)
        fbl.addLayout(gf)
        self.align = QCheckBox("Check and correct overlaps using the audio")
        self.align.setChecked(s.audio_align)
        gf.addRow("", self.align)
        self.align_margin = QDoubleSpinBox()
        self.align_margin.setRange(0.5, 30)
        self.align_margin.setSuffix(" s")
        self.align_margin.setValue(s.align_margin)
        gf.addRow("Audio search window (±)", self.align_margin)
        self.margin_on = QCheckBox("Safety margin: repeat a moment rather than risk losing one")
        self.margin_on.setChecked(s.safety_margin_enabled)
        gf.addRow("", self.margin_on)
        self.margin = QDoubleSpinBox()
        self.margin.setRange(0, 5)
        self.margin.setSingleStep(0.05)
        self.margin.setSuffix(" s")
        self.margin.setValue(s.safety_margin)
        gf.addRow("Safety margin", self.margin)
        self.pad = QDoubleSpinBox()
        self.pad.setRange(0, 30)
        self.pad.setSingleStep(0.25)
        self.pad.setSuffix(" s")
        self.pad.setValue(s.extra_trim_padding)
        gf.addRow("Extra trim padding", self.pad)
        f.addRow(fb)
        self.repeat = QDoubleSpinBox()
        self.repeat.setRange(0, 10)
        self.repeat.setSuffix(" s")
        self.repeat.setValue(s.lossless_max_repeat)
        self.repeat.setToolTip("Lossless keyframe cuts may repeat up to this much footage; beyond it the clip "
                               "is re-encoded for an exact cut. In 'Auto' merges the limit is 0.5 s.")
        f.addRow("Max repeat for keyframe cuts", self.repeat)
        self.seamless = QCheckBox("Tightest seams (allows dropping up to one keyframe interval)")
        self.seamless.setChecked(s.seamless)
        f.addRow("", self.seamless)
        self.ts_end = QComboBox()
        self.ts_end.addItem("When the clip starts", False)
        self.ts_end.addItem("When the clip was saved (its end)", True)
        self.ts_end.setCurrentIndex(1 if s.timestamp_is_end else 0)
        f.addRow("Filename time means", self.ts_end)
        return w

    # ---------------------------------------------------------------- save
    def _reset_savings(self):
        if QMessageBox.question(self, "Reset space saved", "Reset the space saved totals to zero?\n"
                                "Your files are not touched.") == QMessageBox.Yes:
            savings.reset()
            self.savings_info.setText(savings.summary_text())

    def _edit_savings(self):
        d = SavingsEditDialog(savings.load().saved_bytes, self)
        if d.exec() == QDialog.Accepted:
            savings.set_saved(d.value())
            self.savings_info.setText(savings.summary_text())

    def _save(self):
        s = self.s
        enc = self.enc.settings()
        if enc.problems():
            QMessageBox.warning(self, "Compression settings", "\n".join(enc.problems()))
            return
        s.output_dir = self.out.text().strip()
        s.conflict_policy = self.conflict.currentData()
        s.delete_originals = self.delete.isChecked()
        s.permanent_delete = bool(self.permanent.currentData())
        s.count_kept_savings = self.count_kept.isChecked()
        s.name_suffix = self.suffix.text()
        s.add_operation_tags = self.tags.isChecked()
        s.add_trim_range = self.trim_range.isChecked()
        s.prefill_names = self.prefill.isChecked()
        s.add_quality_label = self.qlabel.isChecked()
        s.warn_duplicate_jobs = self.warn_dup.isChecked()
        s.warn_already_processed = self.warn_done.isChecked()
        s.container = self.container.currentData()
        s.exact_cuts = self.exact.isChecked()
        s.smart_cut = self.smart.isChecked()
        s.show_peek = self.peek.isChecked()
        s.rip_format = self.rip.currentData()
        s.ffmpeg_dir = self.ffdir.text().strip()
        s.theme = self.theme.currentData()
        s.on_close = self.on_close.currentData()
        s.auto_delete_logs = self.auto_logs.isChecked()
        s.keep_logs_days = self.keep_logs.value()
        s.encode = enc.to_dict()
        s.presets = self.presets
        s.max_gap = self.gap.value()
        s.audio_align = self.align.isChecked()
        s.align_margin = self.align_margin.value()
        s.safety_margin_enabled = self.margin_on.isChecked()
        s.safety_margin = self.margin.value()
        s.extra_trim_padding = self.pad.value()
        s.lossless_max_repeat = self.repeat.value()
        s.seamless = self.seamless.isChecked()
        s.timestamp_is_end = bool(self.ts_end.currentData())
        s.show_replay_merge = self.show_merge.isChecked()
        s.ui["packet_matching"] = self.packets.isChecked()
        s.game_pause = self.game_pause.isChecked()
        ignore = self.game_ignore.text().strip()
        s.game_pause_ignore = "" if ignore in ("", DEFAULT_IGNORE) else ignore
        s.pause_hotkey = self.hotkey_edit.keySequence().toString(QKeySequence.PortableText)
        s.pause_sounds = self.sounds.isChecked()
        s.save()
        self.accept()


OBS_TIPS = """
<h3>OBS settings that make editing easier</h3>
<p>These are suggestions only. Trimmer never changes OBS.</p>
<h4>Keyframe interval: 1 or 2 seconds</h4>
<p><i>Settings → Output → Recording (Advanced mode) → Keyframe Interval.</i><br>
Lossless trims can only start on a keyframe. With the default "0 (auto)" keyframes can be
several seconds apart, so a lossless cut may start up to that much earlier than you marked.
1–2 s keeps lossless cuts tight with almost no size cost.</p>
<h4>Record at a quality you'd be happy to keep</h4>
<p>Every re-encode loses a little quality. If OBS records with a sensible quality setting
(for example NVENC/AMF/QSV <b>CQP</b> or x264 <b>CRF</b> around 18–23), many clips won't need
compressing at all, and the ones that do start from a better source.</p>
<h4>Consider HEVC (H.265) or AV1 recording</h4>
<p>If your GPU supports it, HEVC or AV1 recording gives much smaller files at the same quality,
straight out of OBS.</p>
<h4>Keep the replay filename format</h4>
<p>The replay merger reads the date and time from names like
<code>Replay 2026-10-20 20-15-03.mp4</code>. If you change OBS's filename format, keep
<code>%CCYY-%MM-%DD %hh-%mm-%ss</code> in it.</p>
<h4>Separate audio tracks</h4>
<p>Recording mic and game/desktop audio on separate tracks lets you rip, drop or mix them
later. Use "Mix tracks into one" when sharing, because most players only play track 1.</p>
"""


class TipsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("OBS settings tips")
        self.resize(560, 560)
        lay = QVBoxLayout(self)
        t = QTextBrowser()
        t.setOpenLinks(False)
        t.setHtml(OBS_TIPS)
        lay.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)


class SavingsEditDialog(QDialog):
    """Asks for a new space saved total in MB, GB or TB."""

    UNITS = (("MB", 1024 ** 2), ("GB", 1024 ** 3), ("TB", 1024 ** 4))

    def __init__(self, current_bytes, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit space saved")
        lay = QVBoxLayout(self)
        lay.addWidget(label("Set the total yourself, for example to count clips you deleted outside Trimmer.\n"
                            "New jobs keep adding to it as usual.", "muted"))
        self.amount = QDoubleSpinBox()
        self.amount.setDecimals(2)
        self.amount.setRange(-1_000_000, 1_000_000)
        self.amount.setMinimumWidth(130)
        self.unit = QComboBox()
        for name, mult in self.UNITS:
            self.unit.addItem(name, mult)
        big = abs(current_bytes) >= 1024 ** 4
        small = abs(current_bytes) < 1024 ** 3
        self.unit.setCurrentIndex(2 if big else 0 if small else 1)
        self.amount.setValue(current_bytes / self.unit.currentData())
        self._mult = self.unit.currentData()
        self.unit.currentIndexChanged.connect(lambda _: self._unit_changed())
        lay.addWidget(hbox(label("Space saved", "muted"), self.amount, self.unit))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _unit_changed(self):
        # keep the same amount of space when switching units
        new = self.unit.currentData()
        self.amount.setValue(self.amount.value() * self._mult / new)
        self._mult = new

    def value(self) -> int:
        return round(self.amount.value() * self.unit.currentData())
