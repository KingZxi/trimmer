"""Replay merge tab: find OBS replay sessions, show how clips overlap,
queue merges or overlap trims."""

import re
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QProgressDialog, QPushButton,
                               QRadioButton, QSplitter, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from ..core import merge as mg
from ..core import usage
from ..core.encode import EncodeSettings
from ..core import ffmpeg as ff
from ..core.ffmpeg import CancelToken
from ..core.render import AUDIO_RIP_FORMATS
from ..core.jobs import JobContext, JobOptions, MergeJob, run_job, session_names
from ..core.util import VIDEO_EXTENSIONS, format_hms, format_hms_ms, parse_timecode, same_file, sanitize_filename
from .theme import colour, set_kind
from .widgets import (OWN_TIP, STAR, HintTree, OptionBinding, card, clean_opts, copy_opts, hbox, label,
                      show_in_folder)
from .clips_tab import CustomEncodeDialog
from .folder_watch import FolderWatcher
from .preview import PreviewDialog
from .queue_checks import check_new_jobs
from .worker import Task


class SessionView(QWidget):
    """Draws each clip of a session as a bar on a shared time axis, so
    overlaps, redundant clips and gaps are visible at a glance. Click a bar
    to watch that clip."""
    clip_clicked = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        self.used = {}              # clip path -> (start, end) used in the merge, or None
        self.marker = None          # (clip path, seconds) of an open viewer
        self.setMinimumHeight(120)
        self.setMouseTracking(True)

    def set_used(self, used):
        self.used = used or {}
        self.update()

    def set_marker(self, path, t):
        self.marker = (path, t) if path is not None else None
        self.update()

    def _clip_at(self, pos):
        s = self.session
        if not s:
            return None
        span = max(1.0, (s.end - s.start).total_seconds())
        scale = (self.width() - 20) / span
        for i, c in enumerate(s.clips):
            x0 = 10 + (c.start - s.start).total_seconds() * scale
            x1 = 10 + (c.end - s.start).total_seconds() * scale
            y = 10 + i * 24
            if x0 - 2 <= pos.x() <= max(x0 + 2, x1) + 2 and y <= pos.y() <= y + 18:
                return c
        return None

    def mouseMoveEvent(self, e):
        c = self._clip_at(e.position())
        self.setCursor(Qt.PointingHandCursor if c else Qt.ArrowCursor)
        self.setToolTip(f"{c.path.name}\nClick to watch" if c else "")

    def mousePressEvent(self, e):
        c = self._clip_at(e.position())
        if c is not None and e.button() == Qt.LeftButton:
            self.clip_clicked.emit(c)

    def set_session(self, s):
        self.session = s
        self.used = {}
        rows = len(s.clips) if s else 1
        self.setMinimumHeight(36 + rows * 24)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = self.session
        if not s:
            p.setPen(colour("faint"))
            p.drawText(self.rect(), Qt.AlignCenter, "Select a session to see how its clips overlap")
            return
        t0 = s.start
        span = max(1.0, (s.end - t0).total_seconds())
        left, right = 10, self.width() - 10
        scale = (right - left) / span
        dropped = {id(c) for c in s.dropped}
        f = p.font()
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1))
        p.setFont(f)
        for i, c in enumerate(s.clips):
            x0 = left + (c.start - t0).total_seconds() * scale
            x1 = left + (c.end - t0).total_seconds() * scale
            y = 10 + i * 24
            rng = self.used.get(str(c.path), "unknown")
            bar = QRectF(x0, y, max(2, x1 - x0), 18)
            p.setPen(Qt.NoPen)
            if id(c) in dropped or rng is None:
                p.setBrush(colour("border_strong"))
                p.drawRoundedRect(bar, 5, 5)
            elif rng == "unknown":
                col = QColor(colour("accent"))
                col.setAlpha(150)
                p.setBrush(col)
                p.drawRoundedRect(bar, 5, 5)
            else:       # repeated footage dim, the used part bright
                dim = QColor(colour("accent"))
                dim.setAlpha(55)
                p.setBrush(dim)
                p.drawRoundedRect(bar, 5, 5)
                bright = QColor(colour("accent"))
                bright.setAlpha(170)
                p.setBrush(bright)
                u0 = x0 + max(0.0, rng[0]) * scale
                u1 = x0 + min(c.duration, rng[1]) * scale
                p.drawRoundedRect(QRectF(u0, y, max(2, u1 - u0), 18), 5, 5)
            p.setPen(colour("text"))
            label = c.path.stem + ("  (redundant)" if id(c) in dropped else "")
            p.drawText(QRectF(x0 + 8, y, max(40, x1 - x0 - 8), 18), Qt.AlignVCenter | Qt.AlignLeft, label)
        if self.marker is not None:
            for i, c in enumerate(s.clips):
                if str(c.path) == self.marker[0]:
                    x = 10 + ((c.start - t0).total_seconds() + min(self.marker[1], c.duration)) * scale
                    y = 10 + i * 24
                    p.setPen(QPen(colour("text"), 2))
                    p.drawLine(QPointF(x, y - 3), QPointF(x, y + 21))
                    p.setPen(Qt.NoPen)
                    p.setBrush(colour("text"))
                    p.drawEllipse(QPointF(x, y - 4), 3.5, 3.5)
        p.setPen(QPen(colour("faint")))
        y = 10 + len(s.clips) * 24 + 4
        p.drawText(QRectF(left, y, 200, 14), Qt.AlignLeft, s.start.strftime("%H:%M:%S"))
        p.drawText(QRectF(right - 200, y, 200, 14), Qt.AlignRight, s.end.strftime("%H:%M:%S"))
        p.end()


PREVIEW_PREFIX = "trimmer_preview_"

# What each session is saved with: the shared options, or a session's own. Track lists are indices.
DEFAULT_OPTS = {
    "mode": "merge", "method": "auto",
    "save_video": True, "drop_tracks": [], "mix": False, "mix_keep": True,      # drop: tracks left out
    "compress": False, "preset": "Custom (Settings)",
    "rip": False, "rip_tracks": [], "rip_fmt": "copy", "rip_video": False,
    "info_file": False, "delete_orig": False,
}


class _Bridge(QObject):
    stage = Signal(str)


class MergeTab(QWidget):
    queue_jobs = Signal(list)
    status = Signal(str)

    def __init__(self, main, parent=None):
        super().__init__(parent)
        self.main = main
        self.paths = []
        self.sessions = []
        self.tasks = []
        self._saved = []            # saved ticks, names and options, applied when detection finishes
        self.shared = copy_opts(DEFAULT_OPTS)   # options of every session without its own
        self.custom = {}            # id(session) -> that session's own options
        self.used = {}              # id(session) -> (ranges, "quick" | "exact" | "estimate")
        self.viewers = []           # open clip viewers
        self.watcher = FolderWatcher(parent=self)
        self.watcher.ready.connect(self._watched)
        self.watcher.lost.connect(self._watch_lost)
        self._build()
        if self.main.settings.show_replay_merge:
            self._start_watch_from_settings()

    def set_shown(self, shown: bool):
        """Showing the tab watches the replay folder again (if that's on); hiding stops it
        without changing the saved setting."""
        if shown:
            self._start_watch_from_settings()
        else:
            self.watcher.stop()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        b_folder = set_kind(QPushButton("Add replay folder…"), "ghost")
        b_folder.clicked.connect(self.main.add_folder_dialog)
        b_files = set_kind(QPushButton("Add replay files…"), "ghost")
        b_files.clicked.connect(self.main.add_files_dialog)
        b_clear = set_kind(QPushButton("Clear"), "ghost")
        b_clear.clicked.connect(self.clear)
        lay.addWidget(hbox(label("Overlapping replays are grouped into sessions. Each becomes one video, "
                                 "or separate clips without the repeats.", "muted"),
                           "stretch", b_folder, b_files, b_clear, stretch_end=False))

        self.watch_box = QCheckBox("Watch folder")
        self.watch_box.setToolTip("Add new replays from this folder automatically as OBS saves them")
        self.watch_box.clicked.connect(lambda on: self._watch_toggled(on))
        self.watch_path = label("", "muted")
        b_watch = set_kind(QPushButton("Choose…"), "ghost")
        b_watch.clicked.connect(lambda: self._choose_watch_folder())
        self.watch_hint = label("", "muted")
        b_desel = set_kind(QPushButton("Deselect all"), "ghost")
        b_desel.setToolTip("Back to the options for all sessions  (Esc in the list)")
        b_desel.clicked.connect(lambda: self.deselect_all())
        lay.addWidget(hbox(self.watch_box, self.watch_path, b_watch, self.watch_hint, "stretch", b_desel,
                           stretch_end=False))

        split = QSplitter(Qt.Vertical)
        self.tree = HintTree()
        self.tree.set_hint("Drop OBS replay clips here", "or the folder they're saved in")
        self.tree.setFrameShape(QFrame.NoFrame)
        self.tree.setHeaderLabels(["Session / clip", "Clips", "Length", "Save as", "Notes"])
        self.tree.setColumnWidth(0, 330)
        self.tree.setColumnWidth(3, 330)
        self.tree.headerItem().setToolTip(0, "Tick the sessions to queue. Shift-click a tick to tick or untick all")
        self.tree.headerItem().setToolTip(3, "Double-click a session (or press F2) to name its output")
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.easy_deselect = True
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemDoubleClicked.connect(self._edit_name)
        self.tree.itemChanged.connect(self._name_changed)
        # Esc while renaming: put the grey default name back
        self.tree.itemDelegate().closeEditor.connect(lambda *_: self._refresh_default_names())
        sc = QShortcut(QKeySequence("F2"), self.tree)
        sc.activated.connect(lambda: self._edit_name(self.tree.currentItem()))
        sc = QShortcut(QKeySequence("Shift+Delete"), self.tree)
        sc.activated.connect(lambda: self.delete_selected())
        self.tree.header().setHighlightSections(False)
        self.tree.itemSelectionChanged.connect(self._sel)
        split.addWidget(self.tree)
        vc, vcl = card(margins=(12, 10, 12, 8), spacing=4)
        self.b_preview = set_kind(QPushButton("Preview merged session"), "ghost")
        self.b_preview.setToolTip("Builds a temporary merged video of the selected session and plays it. "
                                  "Nothing is saved to your output folder.")
        self.b_preview.clicked.connect(self.preview_merged)
        self.view_hint = label("Click a clip to watch it", "muted")
        vcl.addWidget(hbox(self.view_hint, "stretch", self.b_preview, stretch_end=False))
        self.view = SessionView()
        self.view.clip_clicked.connect(lambda c: self.open_clip(c))
        vcl.addWidget(self.view)
        split.addWidget(vc)
        split.setSizes([300, 170])
        lay.addWidget(split, 1)

        self.g_opts = g = QGroupBox("Options for all sessions")
        gl = QVBoxLayout(g)
        gl.setSpacing(8)
        self.r_merge = QRadioButton("Merge into one video")
        self.r_trim = QRadioButton("Keep separate, remove repeated footage")
        self.r_trim.toggled.connect(self._retitle_out)
        self.method = QComboBox()
        self.method.addItem("Auto: exact lossless join when possible, otherwise re-encode", "auto")
        self.method.addItem("Lossless only (fail rather than re-encode)", "lossless")
        self.method.addItem("Always frame-exact re-encode", "exact")
        gl.addWidget(hbox(self.r_merge, self.r_trim, "stretch", label("Method", "muted"), self.method,
                          stretch_end=False))

        self.g_out = QGroupBox("Save the merged video")
        self.g_out.setCheckable(True)
        self.g_out.setChecked(True)
        ol = QVBoxLayout(self.g_out)
        ol.setSpacing(4)
        self.keep_box = QWidget()
        self.keep_lay = QHBoxLayout(self.keep_box)
        self.keep_lay.setContentsMargins(0, 0, 0, 0)
        ol.addWidget(hbox(label("Audio tracks", "muted"), self.keep_box))
        self.mix = QCheckBox("Mix kept tracks into one, so phones and Discord play all of them")
        self.mix_keep = QCheckBox("Also keep the separate tracks")
        self.mix_keep.setChecked(True)
        self.mix.toggled.connect(self.mix_keep.setEnabled)
        self.mix_keep.setEnabled(False)
        ol.addWidget(self.mix)
        indent = QWidget()
        il = QHBoxLayout(indent)
        il.setContentsMargins(25, 0, 0, 0)
        il.addWidget(self.mix_keep)
        ol.addWidget(indent)
        self.compress = QCheckBox("Compress in the same pass")
        self.preset = QComboBox()
        self.preset.setMinimumWidth(190)
        self.b_custom = set_kind(QPushButton("Edit…"), "ghost")
        self.b_custom.clicked.connect(self.edit_custom)
        self.preset.setEnabled(False)
        self.b_custom.setEnabled(False)
        self.compress.toggled.connect(self.preset.setEnabled)
        self.compress.toggled.connect(self.b_custom.setEnabled)
        ol.addWidget(hbox(self.compress, self.preset, self.b_custom))

        self.g_rip = QGroupBox("Rip tracks to separate files")
        self.g_rip.setCheckable(True)
        self.g_rip.setChecked(False)
        rl = QVBoxLayout(self.g_rip)
        rl.setSpacing(4)
        self.rip_box = QWidget()
        self.rip_lay = QHBoxLayout(self.rip_box)
        self.rip_lay.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(hbox(label("Audio tracks", "muted"), self.rip_box))
        self.rip_fmt = QComboBox()
        for k, text in AUDIO_RIP_FORMATS.items():
            self.rip_fmt.addItem(text, k)
        self.rip_video = QCheckBox("Video only (no audio)")
        rl.addWidget(hbox(label("Format", "muted"), self.rip_fmt, "stretch", self.rip_video, stretch_end=False))
        rl.addWidget(label("One file per session when merging, one per trimmed clip when keeping separate",
                           "muted"))
        rl.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self.g_out, 1)
        row.addWidget(self.g_rip, 1)
        gl.addLayout(row)
        self.info_file = QCheckBox("Write an info file showing where each clip landed")
        self.delete_orig = QCheckBox("Delete original clips once the result is saved and checked")
        gl.addWidget(hbox(self.info_file, self.delete_orig))
        lay.addWidget(g)
        self.form = OptionBinding(self._opt_changed)
        self.form.add("mode", lambda: "trim" if self.r_trim.isChecked() else "merge",
                      lambda v: (self.r_trim if v == "trim" else self.r_merge).setChecked(True), self.r_trim.toggled)
        self.form.combo("method", self.method)
        self.form.check("save_video", self.g_out)
        self.form.check("mix", self.mix)
        self.form.check("mix_keep", self.mix_keep)
        self.form.check("compress", self.compress)
        self.form.combo("preset", self.preset, by_text=True)
        self.form.check("rip", self.g_rip)
        self.form.combo("rip_fmt", self.rip_fmt)
        self.form.check("rip_video", self.rip_video)
        self.form.check("info_file", self.info_file)
        self.form.check("delete_orig", self.delete_orig)
        self.keep_checks, self.rip_checks = [], []
        self.b_shared = set_kind(QPushButton("Use shared options"), "ghost")
        self.b_shared.setToolTip("The selected sessions drop their own options (★) and use the options for all sessions")
        self.b_shared.clicked.connect(lambda: self.use_shared())
        self.opts_hint = label("", "muted")
        self.b_queue = set_kind(QPushButton("Add ticked sessions to queue"), "primary")
        self.b_queue.clicked.connect(self.queue)
        lay.addWidget(hbox(self.b_shared, self.opts_hint, "stretch", self.b_queue, stretch_end=False))
        self.refresh_from_settings()

    def refresh_from_settings(self):
        """The settings' defaults become the shared options; sessions with their own keep them."""
        s = self.main.settings
        self.shared.update(mode="trim" if s.merge_mode == "trim" else "merge", method=s.merge_method,
                           info_file=s.generate_clip_log, rip_fmt=s.rip_format, delete_orig=s.delete_originals)
        with self.form.quiet():
            self.preset.clear()
            self.preset.addItem("Custom (Settings)")
            self.preset.addItems(list(s.presets.keys()))
        self._show_opts()
        self._refresh_default_names()

    def _opts(self, st=None) -> JobOptions:
        """Job options for a session's options (default: the shared ones)."""
        st = st or self.shared
        o = JobOptions.from_settings(self.main.settings)
        o.output_dir = self.main.output_dir()
        o.merge_mode = st["mode"]
        o.merge_method = st["method"]
        o.generate_clip_log = st["info_file"]
        o.delete_originals = st["delete_orig"]
        o.rip_format = st["rip_fmt"]
        return o

    # ------------------------------------------------- per-session options
    def _eff(self, s) -> dict:
        """The options a session is saved with: its own, else the shared ones."""
        return self.custom.get(id(s)) or self.shared

    def _selected_sessions(self):
        """Indices of the sessions under the selected rows (a clip row means its session)."""
        n = min(self.tree.topLevelItemCount(), len(self.sessions))
        picked = set()
        for it in self.tree.selectedItems():
            i = self.tree.indexOfTopLevelItem(it if it.parent() is None else it.parent())
            if 0 <= i < n:
                picked.add(i)
        return sorted(picked)

    def _shown_opts(self) -> dict:
        sel = self._selected_sessions()
        return self._eff(self.sessions[sel[0]]) if sel else self.shared

    def _show_opts(self):
        """Show the first selected session's options, or the shared ones when nothing is selected."""
        sel = self._selected_sessions()
        if not sel:
            self.g_opts.setTitle("Options for all sessions")
            self.opts_hint.setText("Select sessions to give them their own options")
        else:
            self.g_opts.setTitle(f"Options for {self.sessions[sel[0]].name}" if len(sel) == 1
                                 else f"Options for {len(sel)} selected sessions")
            self.opts_hint.setText("Changes apply to the selected session" + ("s" if len(sel) > 1 else "")
                                   + " only, ★ marks own options")
        self._build_track_checks()
        self.form.write(self._shown_opts())
        self.b_shared.setEnabled(any(id(self.sessions[i]) in self.custom for i in sel))

    def _change(self, fn):
        """Apply a change: to the shared options, or to each selected session's own (starting from
        what it uses now)."""
        sel = self._selected_sessions()
        if not sel:
            fn(self.shared)
            return
        for i in sel:
            s = self.sessions[i]
            if id(s) not in self.custom:
                self.custom[id(s)] = copy_opts(self.shared)
            fn(self.custom[id(s)])
        self._mark_stars()
        self.b_shared.setEnabled(True)

    def _opt_changed(self, key, value):
        self._change(lambda st: st.__setitem__(key, value))
        if key == "mode":
            self._refresh_default_names()

    def _track_toggled(self, key, t, on):
        if self.form.writing:
            return
        add = on if key == "rip_tracks" else not on

        def fn(st):
            got = set(st[key])
            (got.add if add else got.discard)(t)
            st[key] = sorted(got)
        self._change(fn)

    def _mark_stars(self):
        self.tree.blockSignals(True)
        for i in range(min(self.tree.topLevelItemCount(), len(self.sessions))):
            s, it = self.sessions[i], self.tree.topLevelItem(i)
            own = id(s) in self.custom
            it.setText(0, STAR + s.name if own else s.name)
            it.setToolTip(0, OWN_TIP if own else "")
        self.tree.blockSignals(False)

    def use_shared(self):
        """The selected sessions drop their own options."""
        for i in self._selected_sessions():
            self.custom.pop(id(self.sessions[i]), None)
        self._mark_stars()
        self._show_opts()
        self._refresh_default_names()

    def deselect_all(self):
        self.tree.selectionModel().clear()

    def add_paths(self, paths):
        known = {str(p) for p in self.paths}
        self.paths += [Path(p) for p in paths if str(p) not in known]
        self.detect()

    # ---------------------------------------------------------- watch folder
    def _start_watch_from_settings(self):
        s = self.main.settings
        self.watch_path.setText(s.watch_folder)
        self.watch_box.setChecked(s.watch_enabled and bool(s.watch_folder))
        if self.watch_box.isChecked():
            self._apply_watch()

    def _apply_watch(self):
        """Start or stop watching to match the checkbox and the saved folder."""
        s = self.main.settings
        self.watcher.stop()
        if not (self.watch_box.isChecked() and s.watch_folder):
            self.watch_hint.setText("")
            return
        if self.watcher.start(s.watch_folder):
            self.watch_hint.setText(f"Watching {s.watch_folder}, new replays appear here")
        else:
            self.watch_box.setChecked(False)
            s.watch_enabled = False
            self.watch_hint.setText(f"Can't read {s.watch_folder}, not watching")

    def _watch_toggled(self, on):
        s = self.main.settings
        if on and not s.watch_folder:
            if not self._choose_watch_folder():
                self.watch_box.setChecked(False)
            return
        s.watch_enabled = on
        s.save()
        self._apply_watch()

    def _choose_watch_folder(self) -> bool:
        s = self.main.settings
        d = QFileDialog.getExistingDirectory(self, "Folder OBS saves replays to",
                                             s.watch_folder or s.ui.get("last_dir", ""))
        if not d:
            return False
        s.watch_folder = d
        s.watch_enabled = True
        s.save()
        self.watch_path.setText(d)
        self.watch_box.setChecked(True)
        self._apply_watch()
        return True

    def _watched(self, paths):
        self.add_paths(paths)
        self.status.emit(f"New replay: {', '.join(Path(p).name for p in paths)}")

    def _watch_lost(self, folder):
        self.watch_box.setChecked(False)
        self.main.settings.watch_enabled = False
        self.main.settings.save()
        self.watch_hint.setText(f"{folder} is gone, stopped watching")

    def clear(self):
        self.paths, self.sessions = [], []
        self.used, self.custom = {}, {}
        self.tree.clear()
        self._sel()

    def detect(self):
        mopts = self._opts().merge_options()
        notes = []
        self.status.emit(f"Reading {len(self.paths)} clip(s)…")

        def work():
            clips, skipped = mg.load_clips(self.paths, mopts, notes.append)
            return mg.plan_sessions(clips, mopts, notes.append), skipped

        t = Task(work, parent=self)
        t.done.connect(lambda r: self._show(r[0], r[1], notes))
        t.failed.connect(lambda m: self.status.emit(f"Couldn't read the clips: {m}"))
        self.tasks.append(t)
        t.start()

    def _show(self, sessions, skipped, notes):
        # Ticks, names, own options and the selection carry over to the sessions found again
        self._saved = self.snapshot()["sessions"]
        self.sessions = sessions
        self.used, self.custom = {}, {}
        # No selection or tick signals while the rows are rebuilt: the list has fewer rows than sessions
        self.tree.blockSignals(True)
        try:
            for it in self._fill(sessions):
                it.setSelected(True)
        finally:
            self.tree.blockSignals(False)
        self._saved = []
        self._refresh_default_names()
        self._mark_stars()
        self.refresh_queue_notes()
        self._sel()
        msg = f"Found {len(sessions)} session(s) in {sum(len(s.clips) for s in sessions)} clip(s)."
        if skipped:
            msg += f" Skipped {len(skipped)} file(s) that couldn't be read."
        self.status.emit(msg)

    def _fill(self, sessions):
        """Rebuild the rows; returns the rows that were selected before."""
        self.tree.clear()
        selected = []
        for s in sessions:
            length = (s.end - s.start).total_seconds()
            it = QTreeWidgetItem([s.name, str(len(s.clips)), format_hms(length), "",
                                  "single clip, nothing to merge" if len(s.clips) == 1 else ""])
            it.setFlags(it.flags() | Qt.ItemIsEditable)
            it.setData(3, Qt.UserRole, "")
            it.setData(4, Qt.UserRole, it.text(4))      # the note without the queue status
            it.setCheckState(0, Qt.Checked if len(s.clips) > 1 else Qt.Unchecked)
            if self._apply_saved(it, s):
                selected.append(it)
            dropped = {id(c) for c in s.dropped}
            for c in s.clips:
                note = []
                if id(c) in dropped:
                    note.append("looks fully covered by its neighbours")
                if c.time_source != "filename":
                    note.append("time from file date (no timestamp in name)")
                child = QTreeWidgetItem([c.path.name, "", format_hms(c.duration), "", "; ".join(note)])
                it.addChild(child)
            self.tree.addTopLevelItem(it)
        return selected

    def refresh_queue_notes(self):
        """Show in Notes whether each session is in the queue, and how its job went."""
        self.tree.blockSignals(True)
        try:
            for i, s in enumerate(self.sessions[:self.tree.topLevelItemCount()]):
                it = self.tree.topLevelItem(i)
                text = "; ".join(n for n in (self.main.queue.note_for([c.path for c in s.clips]),
                                             it.data(4, Qt.UserRole)) if n)
                if it.text(4) != text:
                    it.setText(4, text)
                    it.setToolTip(4, text)
        finally:
            self.tree.blockSignals(False)

    # ---------------------------------------------------------- save state
    @staticmethod
    def _session_key(s):
        return sorted(str(c.path) for c in s.clips)

    def snapshot(self) -> dict:
        """The loaded replays, each session's tick, custom name, own options and selection, and the
        shared options, for the save state."""
        sessions = []
        sel = self._selected_sessions()
        for i, s in enumerate(self.sessions[:self.tree.topLevelItemCount()]):
            it = self.tree.topLevelItem(i)
            d = {"clips": self._session_key(s), "checked": it.checkState(0) == Qt.Checked,
                 "name": it.data(3, Qt.UserRole) or "", "selected": i in sel}
            if id(s) in self.custom:
                d["opts"] = copy_opts(self.custom[id(s)])
            sessions.append(d)
        # Saved state not given to a session yet (detection is still running)
        sessions += [{k: v for k, v in d.items() if k != "used"} for d in self._saved if not d.get("used")]
        return {"paths": [str(p) for p in self.paths], "sessions": sessions, "shared": copy_opts(self.shared)}

    def restore(self, data: dict) -> list:
        """Re-add the saved replays; ticks, names and own options are applied after detection.
        Returns the paths that no longer exist."""
        paths = [p for p in data.get("paths", []) if isinstance(p, str)]
        found = [p for p in paths if Path(p).exists()]
        self._saved = [dict(d) for d in data.get("sessions", []) if isinstance(d, dict)]
        if isinstance(data.get("shared"), dict):
            self.shared = clean_opts(data["shared"], self.shared)
            self._show_opts()
        if found:
            self.add_paths(found)
        return [p for p in paths if p not in found]

    def _apply_saved(self, it, s) -> bool:
        """Give a session the saved state of the same clips. A session that has grown since (a new
        replay joined it) keeps its name, own options and selection, but gets the usual tick.
        Returns whether it was selected."""
        key = self._session_key(s)
        left = [d for d in self._saved if not d.get("used") and isinstance(d.get("clips"), list)
                and d["clips"] and all(isinstance(p, str) for p in d["clips"])]
        d = next((d for d in left if d["clips"] == key), None)
        exact = d is not None
        if d is None:
            d = next((d for d in left if set(d["clips"]) <= set(key)), None)
            if d is None:
                return False
        d["used"] = True
        if exact:
            it.setCheckState(0, Qt.Checked if d.get("checked") else Qt.Unchecked)
        name = d.get("name")
        if isinstance(name, str) and name:
            it.setData(3, Qt.UserRole, name)
            it.setText(3, name)
            it.setForeground(3, colour("text"))
        if isinstance(d.get("opts"), dict):
            self.custom[id(s)] = clean_opts(d["opts"], self.shared)
        return d.get("selected") is True

    # ---------------------------------------------------------- naming
    def _default_name(self, i, plain=False):
        s = self.sessions[i]
        if len(s.clips) == 1:
            return s.clips[0].path.stem
        merged, folder = session_names(s)
        if self._eff(s)["mode"] == "trim":
            return folder if plain else folder + "  (folder)"
        return merged

    def _refresh_default_names(self):
        self.tree.blockSignals(True)
        for i in range(min(self.tree.topLevelItemCount(), len(self.sessions))):
            it = self.tree.topLevelItem(i)
            if not it.data(3, Qt.UserRole):
                it.setText(3, self._default_name(i))
                it.setForeground(3, colour("faint"))
        self.tree.blockSignals(False)

    def _edit_name(self, item, _col=None):
        if item is None:
            return
        if item.parent() is not None and _col is not None:     # double-clicked a clip: watch it
            si = self.tree.indexOfTopLevelItem(item.parent())
            ci = item.parent().indexOfChild(item)
            if 0 <= si < len(self.sessions) and 0 <= ci < len(self.sessions[si].clips):
                self.open_clip(self.sessions[si].clips[ci])
            return
        top = item if item.parent() is None else item.parent()
        i = self.tree.indexOfTopLevelItem(top)
        if not top.data(3, Qt.UserRole) and 0 <= i < len(self.sessions):
            self.tree.blockSignals(True)
            top.setText(3, self._default_name(i, plain=True) if self.main.settings.prefill_names else "")
            self.tree.blockSignals(False)
        self.tree.editItem(top, 3)
        ed = self.tree.focusWidget()
        if isinstance(ed, QLineEdit):
            ed.deselect()
            ed.end(False)       # cursor at the end, ready to add to the name

    def _set_all_checked(self, state):
        """Give every session (top-level item, never clip rows) the same tick state."""
        self.tree.blockSignals(True)
        try:
            for i in range(self.tree.topLevelItemCount()):
                self.tree.topLevelItem(i).setCheckState(0, state)
        finally:
            self.tree.blockSignals(False)
        self.tree.viewport().update()
        self._build_track_checks()

    def _name_changed(self, item, col):
        if col == 0 and item.parent() is None:
            self._build_track_checks()
            # Shift-click on a session's tick: copy its new state to every session
            if QApplication.keyboardModifiers() & Qt.ShiftModifier:
                self._set_all_checked(item.checkState(0))
            return
        if col != 3 or item.parent() is not None:
            return
        i = self.tree.indexOfTopLevelItem(item)
        text = item.text(3).strip()
        custom = sanitize_filename(text) if text and text not in (self._default_name(i),
                                                                  self._default_name(i, plain=True)) else ""
        self.tree.blockSignals(True)
        item.setData(3, Qt.UserRole, custom)
        if custom:
            item.setText(3, custom)
            item.setForeground(3, colour("text"))
        self.tree.blockSignals(False)
        if not custom:
            self._refresh_default_names()

    # --------------------------------------------------------- previews
    def _session_of(self, clip):
        return next((s for s in self.sessions if any(c is clip for c in s.clips)), None)

    def open_clip(self, c):
        d = PreviewDialog(self.main, c.path, c.path.name, info=c.info, parent=self)
        d.clip = c
        d.position.connect(lambda t, d=d: self._viewer_moved(d, t))
        d.closed.connect(lambda d=d: self._viewer_closed(d))
        self.viewers.append(d)
        s = self._session_of(c)
        if s is not None:
            self._ensure_used(s)
            self._apply_used(d, s)
        d.show()

    def _viewer_moved(self, d, t):
        if d not in self.viewers:       # closing viewers report one last position; ignore it
            return
        if self.view.session is not None and any(c is d.clip for c in self.view.session.clips):
            self.view.set_marker(str(d.clip.path), t)

    def _viewer_closed(self, d):
        if d in self.viewers:
            self.viewers.remove(d)
        if self.view.marker and self.view.marker[0] == str(d.clip.path):
            self.view.set_marker(None, 0)

    # Which part of each clip is used: instant estimate, then exact in the background.
    def _ensure_used(self, s):
        key = id(s)
        if key in self.used:
            return
        mopts = self._opts().merge_options()
        try:
            self.used[key] = (usage.used_quick(s, mopts), "quick")
        except Exception:
            self.used[key] = ({}, "quick")
        use_packets = bool(self.main.settings.ui.get("packet_matching", True))
        t = Task(lambda: usage.used_exact(s, mopts, use_packets), parent=self)
        t.done.connect(lambda r, key=key: self._used_ready(key, r))
        self.tasks.append(t)
        t.start()

    def _used_ready(self, key, result):
        ranges, how = result
        self.used[key] = (ranges, how)
        s = next((x for x in self.sessions if id(x) == key), None)
        if s is None:
            return
        if self.view.session is s:
            self.view.set_used(ranges)
            self._hint(how)
        for d in self.viewers:
            if any(c is d.clip for c in s.clips):
                self._apply_used(d, s)

    def _hint(self, how):
        self.view_hint.setText("Click a clip to watch it.  Bright = used, dim = repeated in a neighbour"
                               + {"quick": " (estimating…)", "exact": " (exact)",
                                  "estimate": " (estimated)"}.get(how, ""))

    def _apply_used(self, d, s):
        ranges, how = self.used.get(id(s), ({}, "quick"))
        key = str(d.clip.path)
        if key not in ranges:
            return
        rng = ranges[key]
        if len(s.clips) == 1:
            d.set_used(None, "")
            return
        if rng is None:
            d.set_used(None, "None of this clip is used: everything in it is also in the clips around it.")
            return
        a, b = rng
        what = "kept" if self._eff(s)["mode"] == "trim" else "used in the merge"
        tail = {"quick": "Approximate, from the clip timestamps. Working out the exact cut…",
                "exact": "Exact: matched frame by frame with the neighbouring clips.",
                "estimate": "Estimated from timestamps and audio; the clips don't share identical "
                            "frames, so the real cut may differ slightly."}[how]
        d.set_used((a, b), f"Highlighted {format_hms_ms(a)} to {format_hms_ms(b)}: the part {what}; "
                           f"the rest repeats footage from a neighbouring clip. {tail}")

    def _focus_session(self):
        """The session to show: the current row's if it is selected, else the first selected one."""
        sel = self._selected_sessions()
        it = self.tree.currentItem()
        if it is not None and it.isSelected():
            i = self.tree.indexOfTopLevelItem(it if it.parent() is None else it.parent())
            if i in sel:
                return self.sessions[i]
        return self.sessions[sel[0]] if sel else None

    def preview_merged(self):
        s = self._focus_session()
        if s is None:
            QMessageBox.information(self, "No session", "Select a session first.")
            return
        if len(s.clips) == 1:
            self.open_clip(s.clips[0])
            return
        tmp = tempfile.mkdtemp(prefix=PREVIEW_PREFIX)
        o = self._opts(self._eff(s))
        o.output_dir, o.merge_mode, o.delete_originals = tmp, "merge", False
        o.generate_clip_log, o.name_suffix, o.add_operation_tags, o.conflict_policy = False, "", False, "overwrite"
        job = MergeJob(clip_paths=[str(c.path) for c in s.clips], options=o, output_name="Merged preview",
                       title_text=f"Preview of {s.name}")
        token = CancelToken()
        bridge = _Bridge()
        logs = []
        busy = QProgressDialog("Building the merged preview…", "Cancel", 0, 0, self)
        busy.setWindowTitle("Merged preview")
        busy.setWindowModality(Qt.WindowModal)
        busy.setMinimumDuration(300)
        busy.canceled.connect(token.cancel)
        bridge.stage.connect(busy.setLabelText)

        def progress(stage, p):
            pct = f"  {p.fraction * 100:.0f}%" if p.fraction is not None else ""
            bridge.stage.emit(f"{stage}…{pct}")

        def work():
            ctx = JobContext(log=logs.append, progress=progress, resolve_conflict=lambda p: "overwrite",
                             cancel=token)
            res = run_job(job, ctx)
            info = ff.probe(res.outputs[0], use_cache=False) if res.status == "done" and res.outputs else None
            return res, info

        def done(r):
            busy.reset()
            res, info = r
            if res.status != "done" or info is None:
                shutil.rmtree(tmp, ignore_errors=True)
                QMessageBox.warning(self, "Preview failed", "\n".join(res.messages or logs[-3:])
                                    or "The merged preview couldn't be built.")
                return
            joins = []
            for line in logs:
                m = re.match(r"Join at ([\d:.]+):", line)
                if m:
                    try:
                        joins.append(parse_timecode(m.group(1)))
                    except ValueError:
                        pass
            exact = any(line.startswith("Exact overlap found") for line in logs)
            how = ("an exact lossless join (no footage repeated or lost)" if exact
                   else "the audio and timestamp method, as a real merge would")
            PreviewDialog(self.main, res.outputs[0], f"Merged preview: {s.name}", info=info, marks=joins,
                          note=f"Preview only, nothing is saved. Built with {how}. Dashed lines mark "
                               "where one clip hands over to the next.",
                          temp_dir=tmp, parent=self).show()

        def failed(msg):
            busy.reset()
            shutil.rmtree(tmp, ignore_errors=True)
            if not token.cancelled:
                QMessageBox.warning(self, "Preview failed", msg or "The merged preview couldn't be built.")

        t = Task(work, parent=self)
        t.done.connect(done)
        t.failed.connect(failed)
        self.tasks.append(t)
        t.start()

    def _sel(self):
        self._show_opts()
        s = self._focus_session()
        self.b_preview.setEnabled(s is not None)
        if s is not self.view.session:
            self.view.set_session(s)
        if s is None:
            self.view_hint.setText("Click a clip to watch it")
            self.view.set_marker(None, 0)
            return
        if len(s.clips) > 1:
            self._ensure_used(s)
            ranges, how = self.used[id(s)]
            self.view.set_used(ranges)
            self._hint(how)
        else:
            self.view_hint.setText("Click a clip to watch it")
        mk = next((str(d.clip.path) for d in self.viewers if any(c is d.clip for c in s.clips)), None)
        if mk is None:
            self.view.set_marker(None, 0)

    # ------------------------------------------------- send to the Clips tab
    def _selection(self):
        """(clips, sessions) under the selected rows: a session row means all its clips, a clip row just
        that clip. Both come back de-duplicated, in time order."""
        picked = set()
        for it in self.tree.selectedItems():
            top = it if it.parent() is None else it.parent()
            i = self.tree.indexOfTopLevelItem(top)
            if not 0 <= i < len(self.sessions):
                continue
            if it.parent() is None:
                picked.update((i, k) for k in range(len(self.sessions[i].clips)))
            else:
                k = top.indexOfChild(it)
                if 0 <= k < len(self.sessions[i].clips):
                    picked.add((i, k))
        clips = [self.sessions[i].clips[k] for i, k in sorted(picked)]
        sessions = [self.sessions[i] for i in sorted({i for i, _ in picked})]
        return clips, sessions

    def _menu(self, pos):
        it = self.tree.itemAt(pos)
        if it is None:
            return
        if not it.isSelected():
            self.tree.setCurrentItem(it)
            self.tree.clearSelection()
            it.setSelected(True)
        clips, sessions = self._selection()
        if not clips:
            return
        m = QMenu(self)
        m.addAction("Show original in folder" if len(clips) == 1 else "Show originals in folder",
                    lambda: show_in_folder(self, clips[0].path))
        m.addSeparator()
        m.addAction(f"Send {len(clips)} clip{'s' if len(clips) != 1 else ''} to Clips",
                    lambda: self.send_to_clips())
        multi = [s for s in sessions if len(s.clips) > 1]
        if multi:
            m.addAction(f"Merge {len(multi)} session{'s' if len(multi) != 1 else ''}, then send to Clips",
                        lambda: self.merge_then_send())
        m.addSeparator()
        what = f"{len(clips)} clip{'s' if len(clips) != 1 else ''}"
        m.addAction((f"Delete {what} permanently…" if self.main.settings.permanent_delete
                     else f"Move {what} to the Recycle Bin…") + "\tShift+Del", lambda: self.delete_selected())
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _take(self, paths):
        """Remove these clips from Replay merge and find the sessions again. Ticks and names of the
        sessions that stay are kept."""
        gone = {str(p) for p in paths}
        self.paths = [p for p in self.paths if str(p) not in gone]
        if self.paths:
            self.detect()
        else:
            self.clear()

    def send_to_clips(self, clips=None):
        """Move clips (default: the selection) to the Clips tab, each as its own entry."""
        if clips is None:
            clips = self._selection()[0]
        if not clips:
            return
        paths = [c.path for c in clips]
        self._take(paths)
        self.main.send_to_clips(paths)

    def release(self, paths):
        """Close the viewers of files that are about to be deleted."""
        for d in [d for d in self.viewers if any(same_file(d.clip.path, p) for p in paths)]:
            d.close()

    def delete_selected(self, clips=None):
        """Move clips (default: the selection) to the Recycle Bin (or delete them, as Settings say) and
        take them out of Replay merge."""
        if clips is None:
            clips = self._selection()[0]
        if not clips:
            return
        doomed = {str(c.path) for c in clips}
        for d in [d for d in self.viewers if str(d.clip.path) in doomed]:
            d.close()                   # an open viewer holds the file
        gone = self.main.delete_originals([c.path for c in clips], self)
        if gone:
            self._take(gone)

    def merge_then_send(self, sessions=None):
        """Merge each multi-clip session (default: those in the selection) with a plain lossless merge,
        and when a merge is done add the result to the Clips tab as one clip."""
        if sessions is None:
            sessions = self._selection()[1]
        sessions = [s for s in sessions if len(s.clips) > 1]
        if not sessions:
            return
        jobs, groups = [], {}
        for s in sessions:
            o = self._opts(self._eff(s))
            o.merge_mode, o.delete_originals, o.generate_clip_log = "merge", False, False
            name = self.tree.topLevelItem(self.sessions.index(s)).data(3, Qt.UserRole) or ""
            job = MergeJob(clip_paths=[str(c.path) for c in s.clips], title_text=f"Session {s.name}",
                           options=o, output_name=name)
            jobs.append(job)
            groups[id(job)] = [c.path for c in s.clips]
        jobs = check_new_jobs(self.main, jobs, {str(c.path): c.info for s in sessions for c in s.clips},
                              self, check_processed=False)
        if not jobs:
            return
        q = self.main.queue
        for job in jobs:
            q.on_finished[id(job)] = lambda item, paths=groups[id(job)]: self._merge_finished(item, paths)
        self._take([p for job in jobs for p in groups[id(job)]])
        q.add_jobs(jobs, start=True)
        if not q.is_running():          # runs now even when the queue isn't set to start by itself
            q.start()
        self.status.emit(f"Merging {len(jobs)} session{'s' if len(jobs) != 1 else ''}; the result "
                         f"goes to Clips when it's done.")

    def _merge_finished(self, item, paths):
        outs = [p for p in (item.result.outputs if item.result else []) if Path(p).suffix.lower() in VIDEO_EXTENSIONS]
        outs = [p for p in outs if Path(p).exists()]
        if item.status == "done" and outs:
            self.main.send_to_clips(outs[:1])
            self.status.emit(f"Merged {len(paths)} clips into {Path(outs[0]).name} and sent it to Clips.")
        else:
            self.add_paths([p for p in paths if Path(p).exists()])
            self.status.emit(f"The merge didn't finish ({item.status}); the {len(paths)} clips are back in "
                             f"Replay merge.")

    def _retitle_out(self):
        self.g_out.setTitle("Save the trimmed videos" if self.r_trim.isChecked() else "Save the merged video")

    def _ticked(self):
        """Ticked sessions. While the list is being rebuilt it can have fewer rows than sessions."""
        items = [self.tree.topLevelItem(i) for i in range(len(self.sessions))]
        return [s for s, it in zip(self.sessions, items) if it is not None and it.checkState(0) == Qt.Checked]

    def _track_sessions(self):
        """Sessions whose tracks the checkboxes describe: the selected ones, else the ticked ones, else all."""
        sel = self._selected_sessions()
        if sel:
            return [self.sessions[i] for i in sel]
        return self._ticked() or list(self.sessions)

    def _build_track_checks(self):
        """One keep and one rip checkbox per audio track of the sessions in play, ticked as the
        options shown say."""
        st = self._shown_opts()
        for lay in (self.keep_lay, self.rip_lay):
            while lay.count():
                w = lay.takeAt(0).widget()
                if w:
                    w.deleteLater()
        clips = [c for s in self._track_sessions() for c in s.clips]
        ref = max((c.info.audio for c in clips), key=len, default=[])
        n = len(ref)
        self.keep_checks, self.rip_checks = [], []
        for t in range(n):
            text = f"{t + 1}: {ref[t].label}"
            k = QCheckBox(text)
            k.setChecked(t not in st["drop_tracks"])
            k.toggled.connect(lambda on, t=t: self._track_toggled("drop_tracks", t, on))
            self.keep_lay.addWidget(k)
            self.keep_checks.append(k)
            rc = QCheckBox(text)
            rc.setChecked(t in st["rip_tracks"])
            rc.toggled.connect(lambda on, t=t: self._track_toggled("rip_tracks", t, on))
            self.rip_lay.addWidget(rc)
            self.rip_checks.append(rc)
        if n == 0:
            self.keep_lay.addWidget(QLabel("(no audio)"))
            self.rip_lay.addWidget(QLabel("(no audio)"))
        self.keep_lay.addStretch(1)
        self.rip_lay.addStretch(1)

    def _enc(self, st) -> EncodeSettings:
        name = st["preset"]
        s = self.main.settings
        return EncodeSettings.from_dict(s.presets[name]) if name in s.presets else s.encode_settings()

    def edit_custom(self):
        """Same as the Clips tab: start from the preset shown, save the
        result as the Custom settings and switch to them."""
        dlg = CustomEncodeDialog(self._enc({"preset": self.preset.currentText()}), self.main.encoders, self)
        if dlg.exec():
            self.main.settings.encode = dlg.form.settings().to_dict()
            self.main.settings.save()
            self.preset.setCurrentIndex(0)
            self.compress.setChecked(True)

    def queue(self):
        """Queue each ticked session with its own options, or the shared ones."""
        chosen = [(s, self._eff(s)) for s in self._ticked()]
        if not chosen:
            QMessageBox.information(self, "Nothing ticked", "Tick at least one session.")
            return
        for s, st in chosen:
            enc = self._enc(st)
            if st["save_video"] and st["compress"] and enc.problems():
                QMessageBox.warning(self, "Compression settings", "\n".join(enc.problems()))
                return
        plans = []
        for s, st in chosen:
            n_tracks = min(len(c.info.audio) for c in s.clips)
            rips = [t for t in st["rip_tracks"] if t < n_tracks] if st["rip"] else []
            rip_video = st["rip"] and st["rip_video"]
            if not st["save_video"] and not rips and not rip_video:
                what = "Save the trimmed videos" if st["mode"] == "trim" else "Save the merged video"
                QMessageBox.information(self, "Nothing to do",
                                        f"Nothing to save for session {s.name}. "
                                        f"Tick '{what}' or choose tracks to rip.")
                return
            plans.append((s, st, n_tracks, rips, rip_video))
        n = sum(len(s.clips) for s, st in chosen if st["delete_orig"])
        if n:
            where = "permanently deleted" if self.main.settings.permanent_delete else "moved to the Recycle Bin"
            if QMessageBox.question(self, "Delete originals?",
                                    f"{n} original clip(s) will be {where} once each result is saved and "
                                    f"checked. Continue?") != QMessageBox.Yes:
                return
        jobs = []
        for s, st, n_tracks, rips, rip_video in plans:
            tracks = [t for t in range(n_tracks) if t not in st["drop_tracks"]]
            jobs.append(MergeJob(
                clip_paths=[str(c.path) for c in s.clips], make_video=st["save_video"],
                compress=st["save_video"] and st["compress"], encode=self._enc(st).to_dict(),
                audio_tracks=None if len(tracks) == n_tracks else tracks,
                mix_tracks=tracks if st["mix"] and len(tracks) >= 2 else None,
                keep_mixed_originals=st["mix_keep"],
                rip_audio_tracks=rips, rip_video=rip_video,
                title_text=f"Session {s.name}", options=self._opts(st),
                output_name=self.tree.topLevelItem(self.sessions.index(s)).data(3, Qt.UserRole) or ""))
        jobs = check_new_jobs(self.main, jobs, {str(c.path): c.info for s, _ in chosen for c in s.clips}, self)
        if jobs:
            self.queue_jobs.emit(jobs)
