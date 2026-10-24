"""Settings dialog and the OBS tips page."""

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QMessageBox, QPushButton, QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from ..core import ffmpeg as ff
from ..core import savings
from ..core.encode import EncodeSettings
from ..core.render import AUDIO_RIP_FORMATS
from ..core.settings import default_presets
from ..core.util import app_data_dir
from .widgets import EncodeForm, hbox, label


class SettingsDialog(QDialog):
    def __init__(self, settings, encoders, parent=None):
        super().__init__(parent)
        self.s = settings
        self.encoders = encoders
        self.setWindowTitle("Settings")
        self.resize(620, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 16)
        lay.setSpacing(12)
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        lay.addWidget(tabs)
        tabs.addTab(self._general(), "General")
        tabs.addTab(self._compression(), "Compression")
        tabs.addTab(self._merge(), "Replay merge")
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setProperty("kind", "primary")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

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
        b_reset = QPushButton("Reset")
        b_reset.clicked.connect(lambda: self._reset_savings())
        f.addRow("Space saved", hbox(self.savings_info, "stretch", b_reset, stretch_end=False))
        self.suffix = QLineEdit(s.name_suffix)
        self.suffix.setPlaceholderText("e.g. _edit")
        f.addRow("Add to output names", self.suffix)
        self.tags = QCheckBox("Add what was done to names, e.g. (trimmed), (no audio)")
        self.tags.setChecked(s.add_operation_tags)
        f.addRow("", self.tags)
        self.prefill = QCheckBox("When renaming, start with the original name so you can add to it")
        self.prefill.setChecked(s.prefill_names)
        f.addRow("", self.prefill)
        self.qlabel = QCheckBox("Add compression details to compressed file names, e.g. (LOSSY), (CQ28, H265 NVENC)")
        self.qlabel.setChecked(s.add_quality_label)
        f.addRow("", self.qlabel)
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
        f.addRow("Settings and logs", QLabel(str(app_data_dir())))
        return w

    def _pick(self, line):
        d = QFileDialog.getExistingDirectory(self, "Choose folder", line.text())
        if d:
            line.setText(d)

    # --------------------------------------------------------- compression
    def _compression(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Custom settings (used when 'Custom (Settings)' is picked):"))
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
        self.packets = QCheckBox("Match clips frame by frame (exact, lossless) when they share footage")
        self.packets.setChecked(True)
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
        s.prefill_names = self.prefill.isChecked()
        s.add_quality_label = self.qlabel.isChecked()
        s.container = self.container.currentData()
        s.exact_cuts = self.exact.isChecked()
        s.smart_cut = self.smart.isChecked()
        s.show_peek = self.peek.isChecked()
        s.rip_format = self.rip.currentData()
        s.ffmpeg_dir = self.ffdir.text().strip()
        s.theme = self.theme.currentData()
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
        s.ui["packet_matching"] = self.packets.isChecked()
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
