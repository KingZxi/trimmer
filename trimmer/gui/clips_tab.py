"""Clips tab: drop clips in, preview, mark trims, choose what to save."""

import hashlib
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMenu, QMessageBox,
                               QPushButton, QScrollArea, QSplitter, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ..core import ffmpeg as ff
from ..core import peek
from ..core.encode import EncodeSettings, predict_size, resolve_quality
from ..core.jobs import ClipJob, JobOptions
from ..core.render import AUDIO_RIP_FORMATS
from ..core.util import format_hms, format_hms_ms, format_size, sanitize_filename
from .queue_checks import check_new_jobs
from .theme import set_kind
from .widgets import (OWN_TIP, STAR, EncodeForm, HintTable, OptionBinding, Timeline, TrackPicker, card, clean_opts,
                      clearable, copy_opts, hbox, label)

try:        # Windows Smart App Control can block Qt's multimedia DLLs
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAVE_PLAYBACK = True
except (ImportError, OSError):
    from .fallback_player import FramePlayer as QMediaPlayer
    from .fallback_player import FrameView as QVideoWidget
    QAudioOutput = None
    HAVE_PLAYBACK = False
from .worker import Task

COLS = ["Name", "Length", "Size", "Video", "Audio tracks", "Trim", "Notes"]

# What each clip is saved with: the shared options, or a clip's own. Track lists are indices.
DEFAULT_OPTS = {
    "save_video": True, "drop_tracks": [], "mix": False, "mix_keep": True,      # drop: tracks left out
    "compress": False, "preset": "Custom (Settings)", "exact": False,
    "rip_tracks": [], "rip_fmt": "copy", "rip_video": False, "delete_orig": False,
}


class ClipEntry:
    def __init__(self, path: Path, info):
        self.path = path
        self.info = info
        self.segments = []
        self.note = ""
        self.snap_note = ""
        self.snap_marks = []          # keyframes the cuts would snap back to
        self.out_name = ""          # user's name for the outputs; "" = from the original
        self.opts = None            # its own options, or None for the shared ones


def quick_fingerprint(path: Path) -> str:
    """Size + first and last MB: catches the same clip added twice, even
    under another name, without reading whole files."""
    h = hashlib.sha1()
    size = path.stat().st_size
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if size > (2 << 20):
            f.seek(-(1 << 20), 2)
            h.update(f.read(1 << 20))
    return h.hexdigest()


class CustomEncodeDialog(QDialog):
    def __init__(self, settings: EncodeSettings, encoders, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Compression settings")
        lay = QVBoxLayout(self)
        self.form = EncodeForm(encoders)
        self.form.set_settings(settings)
        lay.addWidget(self.form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class ClipsTab(QWidget):
    queue_jobs = Signal(list)
    status = Signal(str)

    def __init__(self, main, parent=None):
        super().__init__(parent)
        self.main = main
        self.entries = []
        self.fingerprints = {}
        self.current = None
        self.tasks = []
        self._restore = {}            # path -> saved edit state, applied as the clips finish loading
        self._restore_select = []
        self.shared = copy_opts(DEFAULT_OPTS)   # options of every clip without its own
        self._build()

    # ------------------------------------------------------------- layout
    def _build(self):
        split = QSplitter(Qt.Horizontal, self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.addWidget(split)

        # ---- left: the clip list
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(10)
        self.table = HintTable(0, len(COLS))
        self.table.set_hint("Drop clips here", "Single files, a handful, or whole folders")
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.easy_deselect = True
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setFrameShape(QFrame.NoFrame)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(True)
        hdr.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hdr.setHighlightSections(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.setWordWrap(False)
        for c, w in enumerate((200, 58, 70, 130, 120, 96)):
            self.table.setColumnWidth(c, w)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        ll.addWidget(self.table, 1)
        b_add = set_kind(QPushButton("Add files…"), "ghost")
        b_add.clicked.connect(self.main.add_files_dialog)
        b_folder = set_kind(QPushButton("Add folder…"), "ghost")
        b_folder.clicked.connect(self.main.add_folder_dialog)
        b_rm = set_kind(QPushButton("Remove"), "ghost")
        b_rm.clicked.connect(self.remove_selected)
        b_clear = set_kind(QPushButton("Clear"), "ghost")
        b_clear.clicked.connect(self.clear)
        b_desel = set_kind(QPushButton("Deselect all"), "ghost")
        b_desel.setToolTip("Back to the options for all clips  (Esc in the list)")
        b_desel.clicked.connect(lambda: self.deselect_all())
        ll.addWidget(hbox(b_add, b_folder, "stretch", b_desel, b_rm, b_clear, stretch_end=False))
        split.addWidget(left)

        # ---- right: preview, trim, output options
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(10)

        pv, pvl = card(margins=(0, 0, 0, 0), spacing=0)
        pv.setObjectName("video")
        self.video = QVideoWidget()
        self.video.setMinimumHeight(220)
        self.player = QMediaPlayer(self)
        if QAudioOutput is not None:
            self.audio_out = QAudioOutput(self)
            self.player.setAudioOutput(self.audio_out)
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self._pos_changed)
        self.player.durationChanged.connect(lambda d: self.timeline.set_state(duration=d / 1000))
        self.player.tracksChanged.connect(self._tracks_changed)
        pvl.addWidget(self.video)
        rl.addWidget(pv, 4)

        self.timeline = Timeline()
        self.timeline.seek.connect(lambda t: self.player.setPosition(int(t * 1000)))
        rl.addWidget(self.timeline)

        self.b_play = QPushButton("Play")
        self.b_play.setMinimumWidth(76)
        self.b_play.clicked.connect(self.toggle_play)
        b_back = set_kind(QPushButton("‹"), "icon")
        b_back.setToolTip("Previous frame  (Left)")
        b_back.clicked.connect(lambda: self.step(-1))
        b_fwd = set_kind(QPushButton("›"), "icon")
        b_fwd.setToolTip("Next frame  (Right)")
        b_fwd.clicked.connect(lambda: self.step(1))
        self.time_label = label("0:00.000", "mono")
        self.preview_track = TrackPicker(self.main.track_prefs)
        self.preview_track.track_chosen.connect(self.player.setActiveAudioTrack)
        hear = label("Hear", "muted")
        b_in = QPushButton("Mark in")
        b_in.setToolTip("Start a kept part here  (I)")
        b_in.clicked.connect(self.mark_in)
        b_out = QPushButton("Mark out")
        b_out.setToolTip("End the kept part here  (O)")
        b_out.clicked.connect(self.mark_out)
        rl.addWidget(hbox(self.b_play, b_back, b_fwd, self.time_label, "stretch",
                          hear, self.preview_track, b_in, b_out, stretch_end=False))
        if not HAVE_PLAYBACK:       # still-frame preview has no sound
            hear.hide()
            self.preview_track.hide()

        parts, partsl = card(spacing=6)
        b_seg_rm = set_kind(QPushButton("Remove part"), "ghost")
        b_seg_rm.clicked.connect(self.remove_segment)
        b_seg_clear = set_kind(QPushButton("Keep whole clip"), "ghost")
        b_seg_clear.clicked.connect(self.clear_segments)
        partsl.addWidget(hbox(label("Kept parts", "h2"), "stretch", b_seg_rm, b_seg_clear, stretch_end=False))
        self.seg_list = QListWidget()
        self.seg_list.setFrameShape(QFrame.NoFrame)
        self.seg_list.setMaximumHeight(64)
        partsl.addWidget(self.seg_list)
        self.trim_info = label("", "muted", wrap=True)
        partsl.addWidget(self.trim_info)
        rl.addWidget(parts)

        self.out_name = QLineEdit()
        clearable(self.out_name)
        self.out_name.setToolTip("Name for the saved video and ripped tracks (F2). Leave empty to keep "
                                 "the original name.")
        self.out_name.textEdited.connect(self._rename)
        rl.addWidget(hbox(label("Save as", "muted"), self.out_name, stretch_end=False))

        # Output options (scrolls when the window is short)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        acts = QWidget()
        al = QVBoxLayout(acts)
        al.setContentsMargins(0, 0, 4, 0)
        al.setSpacing(10)
        self.opts_title = label("Options for all clips", "h2")
        self.b_shared = set_kind(QPushButton("Use shared options"), "ghost")
        self.b_shared.setToolTip("The selected clips drop their own options (★) and use the options for all clips")
        self.b_shared.clicked.connect(lambda: self.use_shared())
        al.addWidget(hbox(self.opts_title, "stretch", self.b_shared, stretch_end=False))

        g_out = QGroupBox("Save the edited video")
        g_out.setCheckable(True)
        g_out.setChecked(True)
        self.g_out = g_out
        gl = QVBoxLayout(g_out)
        gl.setSpacing(4)
        self.track_box = QWidget()
        self.track_lay = QHBoxLayout(self.track_box)
        self.track_lay.setContentsMargins(0, 0, 0, 0)
        gl.addWidget(hbox(label("Audio tracks", "muted"), self.track_box))
        self.mix = QCheckBox("Mix kept tracks into one, so phones and Discord play all of them")
        self.mix_keep = QCheckBox("Also keep the separate tracks")
        self.mix_keep.setChecked(True)
        self.mix.toggled.connect(self.mix_keep.setEnabled)
        self.mix_keep.setEnabled(False)
        gl.addWidget(self.mix)
        indent = QWidget()
        il = QHBoxLayout(indent)
        il.setContentsMargins(25, 0, 0, 0)
        il.addWidget(self.mix_keep)
        gl.addWidget(indent)
        self.compress = QCheckBox("Compress")
        self.preset = QComboBox()
        self.preset.setMinimumWidth(190)
        self.b_custom = set_kind(QPushButton("Edit…"), "ghost")
        self.b_custom.clicked.connect(self.edit_custom)
        self.compress.toggled.connect(self._update_info)
        self.compress.toggled.connect(self.preset.setEnabled)
        self.compress.toggled.connect(self.b_custom.setEnabled)
        self.preset.setEnabled(False)
        self.b_custom.setEnabled(False)
        gl.addWidget(hbox(self.compress, self.preset, self.b_custom))
        self.exact = QCheckBox("Frame-exact cuts (smart cut: only the frames at each cut are re-encoded)")
        self.exact.toggled.connect(self._update_info)
        gl.addWidget(self.exact)
        al.addWidget(g_out)

        g_rip = QGroupBox("Rip tracks to separate files")
        rgl = QVBoxLayout(g_rip)
        rgl.setSpacing(4)
        self.rip_box = QWidget()
        self.rip_lay = QHBoxLayout(self.rip_box)
        self.rip_lay.setContentsMargins(0, 0, 0, 0)
        self.rip_fmt = QComboBox()
        for k, text in AUDIO_RIP_FORMATS.items():
            self.rip_fmt.addItem(text, k)
        rgl.addWidget(hbox(label("Audio tracks", "muted"), self.rip_box))
        self.rip_video = QCheckBox("Video only (no audio)")
        rgl.addWidget(hbox(label("Format", "muted"), self.rip_fmt, "stretch", self.rip_video, stretch_end=False))
        al.addWidget(g_rip)
        self.delete_orig = QCheckBox("Delete originals after everything is saved and checked")
        al.addWidget(self.delete_orig)
        al.addStretch(1)
        scroll.setWidget(acts)
        rl.addWidget(scroll, 3)
        self.form = OptionBinding(self._opt_changed)
        self.form.check("save_video", self.g_out)
        self.form.check("mix", self.mix)
        self.form.check("mix_keep", self.mix_keep)
        self.form.check("compress", self.compress)
        self.form.combo("preset", self.preset, by_text=True)
        self.form.check("exact", self.exact)
        self.form.combo("rip_fmt", self.rip_fmt)
        self.form.check("rip_video", self.rip_video)
        self.form.check("delete_orig", self.delete_orig)
        self.keep_checks, self.rip_checks = [], []

        # Pinned action bar
        b_est = set_kind(QPushButton("Estimate size"), "ghost")
        b_est.setToolTip("Encodes a few short samples to predict the output size")
        b_est.clicked.connect(self.estimate)
        self.estimate_label = label("", "muted", wrap=True)
        self.b_queue_sel = set_kind(QPushButton("Add selected to queue"), "primary")
        self.b_queue_sel.clicked.connect(lambda: self.queue(selected_only=True))
        b_queue_all = QPushButton("Add all")
        b_queue_all.clicked.connect(lambda: self.queue(selected_only=False))
        rl.addWidget(hbox(b_est, self.estimate_label, "stretch", b_queue_all, self.b_queue_sel,
                          stretch_end=False))
        split.addWidget(right)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 5)
        split.setSizes([540, 700])

        for key, fn in (("I", self.mark_in), ("O", self.mark_out), ("Space", self.toggle_play),
                        ("Left", lambda: self.step(-1)), ("Right", lambda: self.step(1)),
                        ("J", lambda: self.jump(-5)), ("L", lambda: self.jump(5)),
                        ("Delete", self.remove_selected), ("Shift+Delete", lambda: self.delete_selected()),
                        ("F2", lambda: (self.out_name.setFocus(), self.out_name.end(False)))):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        self.refresh_from_settings()

    # ------------------------------------------------------------ settings
    def refresh_from_settings(self):
        """The settings' defaults become the shared options; clips with their own keep them."""
        s = self.main.settings
        self.shared.update(exact=s.exact_cuts, delete_orig=s.delete_originals, rip_fmt=s.rip_format)
        with self.form.quiet():
            self.preset.clear()
            self.preset.addItem("Custom (Settings)")
            self.preset.addItems(list(s.presets.keys()))
        self.exact.setText("Frame-exact cuts (smart cut: only the frames at each cut are re-encoded)"
                           if s.smart_cut else "Frame-exact cuts (re-encodes at high quality when not compressing)")
        self.delete_orig.setText("Delete originals after everything is saved and checked ("
                                 + ("permanently" if s.permanent_delete else "to Recycle Bin") + ")")
        self._show_opts(self.selected_entries())

    def encode_settings(self, name=None) -> EncodeSettings:
        """A preset's settings (default: the one shown)."""
        name = self.preset.currentText() if name is None else name
        s = self.main.settings
        if name in s.presets:
            return EncodeSettings.from_dict(s.presets[name])
        return s.encode_settings()

    def edit_custom(self):
        dlg = CustomEncodeDialog(self.encode_settings(), self.main.encoders, self)
        if dlg.exec():
            self.main.settings.encode = dlg.form.settings().to_dict()
            self.main.settings.save()
            self.preset.setCurrentIndex(0)
            self.compress.setChecked(True)

    # --------------------------------------------------------------- list
    def add_paths(self, paths):
        """Probe in the background so big drops don't freeze the window."""
        paths = [Path(p) for p in paths]
        self.status.emit(f"Reading {len(paths)} file(s)…")

        def work():
            out = []
            for p in paths:
                try:
                    out.append((p, quick_fingerprint(p), ff.probe(p), None))
                except (OSError, ff.FFmpegError) as e:
                    out.append((p, None, None, str(e)))
            return out

        t = Task(work, parent=self)
        t.done.connect(self._add_probed)
        self.tasks.append(t)
        t.start()

    def _add_probed(self, results):
        added, dups, long_paths, errors = 0, 0, 0, []
        for p, fp, info, err in results:
            if err:
                errors.append(f"{p.name}: {err}")
                continue
            if fp in self.fingerprints:
                dups += 1
                continue
            if not info.has_video and not info.audio:
                continue
            e = ClipEntry(p, info)
            if len(str(p)) > 240:
                e.note = "very long path"
                long_paths += 1
            self.fingerprints[fp] = e
            self.entries.append(e)
            self._apply_saved(e)
            self._add_row(e)
            added += 1
        msg = f"Added {added} clip(s)."
        if dups:
            msg += f" Skipped {dups} duplicate(s) already in the list."
        if long_paths:
            msg += " Some paths are very long; if a save fails, move them to a shorter folder."
        if errors:
            msg += f" {len(errors)} file(s) couldn't be read: " + "; ".join(errors[:3])
        self.status.emit(msg)
        if self._restore_select:
            rows = [i for i, e in enumerate(self.entries) if str(e.path) in self._restore_select]
            if rows:
                self._restore_select = []
                self.table.selectRow(rows[0])
        if added and not self.selected_entries():
            self.table.selectRow(self.table.rowCount() - added)

    # ---------------------------------------------------------- save state
    def snapshot(self) -> dict:
        """The list and each clip's edits, for the save state."""
        entries = []
        for e in self.entries:
            d = {"path": str(e.path), "segments": [list(s) for s in e.segments], "out_name": e.out_name}
            if e.opts is not None:
                d["opts"] = copy_opts(e.opts)
            entries.append(d)
        # Clips from a restore that are still loading
        entries += [dict(d) for d in self._restore.values()]
        return {"entries": entries, "selected": [str(e.path) for e in self.selected_entries()],
                "shared": copy_opts(self.shared)}

    def restore(self, data: dict) -> list:
        """Re-add the saved clips (edits are applied as each one finishes
        loading). Returns the paths that no longer exist."""
        found, missing = [], []
        for d in data.get("entries", []):
            if not isinstance(d, dict) or not isinstance(d.get("path"), str):
                continue
            if Path(d["path"]).exists():
                found.append(Path(d["path"]))
                self._restore[d["path"]] = d
            else:
                missing.append(d["path"])
        if isinstance(data.get("shared"), dict):
            self.shared = clean_opts(data["shared"], self.shared)
            self._show_opts(self.selected_entries())
        sel = data.get("selected")
        self._restore_select = [p for p in sel if p in self._restore] if isinstance(sel, list) else []
        if found:
            self.add_paths(found)
        return missing

    def _apply_saved(self, e: ClipEntry):
        d = self._restore.pop(str(e.path), None)
        if not d:
            return
        try:
            e.segments = [(float(a), None if b is None else float(b)) for a, b in d.get("segments", [])]
        except (TypeError, ValueError):
            e.segments = []
        if isinstance(d.get("out_name"), str):
            e.out_name = sanitize_filename(d["out_name"]) if d["out_name"] else ""
        if isinstance(d.get("opts"), dict):
            e.opts = clean_opts(d["opts"], self.shared)

    def _add_row(self, e: ClipEntry):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self._fill_row(r, e)

    def _fill_row(self, r, e: ClipEntry):
        i = e.info
        tracks = ", ".join(a.label for a in i.audio) or "none"
        trim = "whole" if not e.segments else f"{len(e.segments)} part(s), {format_hms(self._kept(e))}"
        vals = [(STAR if e.opts is not None else "") + e.path.name + (f"  →  {e.out_name}" if e.out_name else ""),
                format_hms(i.duration), format_size(i.size),
                f"{i.resolution} {i.fps:.0f}fps {i.video_codec}" if i.has_video else "audio only",
                f"{len(i.audio)}: {tracks}", trim,
                "; ".join(n for n in (self.main.queue.note_for([e.path]), e.note) if n)]
        for c, v in enumerate(vals):
            item = QTableWidgetItem(v)
            item.setToolTip(v if c else str(e.path) + (f"\n{OWN_TIP}" if e.opts is not None else ""))
            self.table.setItem(r, c, item)

    def refresh_queue_notes(self):
        """Show in Notes whether each clip is in the queue, and how its job went."""
        col = len(COLS) - 1
        for r, e in enumerate(self.entries):
            text = "; ".join(n for n in (self.main.queue.note_for([e.path]), e.note) if n)
            item = self.table.item(r, col)
            if item is not None and item.text() != text:
                item.setText(text)
                item.setToolTip(text)

    def _kept(self, e):
        if not e.segments:
            return e.info.duration
        return sum((e.info.duration if b is None else b) - a for a, b in e.segments)

    def selected_entries(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.entries[r] for r in rows if r < len(self.entries)]

    def remove_selected(self):
        self._drop(self.selected_entries())

    def _drop(self, entries):
        """Take these clips out of the list."""
        for e in entries:
            r = self.entries.index(e)
            self.entries.pop(r)
            self.fingerprints = {k: v for k, v in self.fingerprints.items() if v is not e}
            self.table.removeRow(r)
            if e is self.current:
                self._load(None)

    def delete_selected(self, entries=None):
        """Move the selected clips' files to the Recycle Bin (or delete them, as Settings say) and take
        them out of the list."""
        entries = self.selected_entries() if entries is None else entries
        if not entries:
            return
        if self.current in entries:
            self._load(None)            # the preview lets go of the file first
        gone = {str(p) for p in self.main.delete_originals([e.path for e in entries], self)}
        self._drop([e for e in entries if str(e.path) in gone])

    def _menu(self, pos):
        r = self.table.rowAt(pos.y())
        if r < 0:
            return
        if not self.table.item(r, 0).isSelected():
            self.table.selectRow(r)
        n = len(self.selected_entries())
        what = f"{n} clip{'s' if n != 1 else ''}"
        m = QMenu(self)
        m.addAction(f"Remove {what} from the list", self.remove_selected)
        m.addAction((f"Delete {what} permanently…" if self.main.settings.permanent_delete
                     else f"Move {what} to the Recycle Bin…") + "\tShift+Del", lambda: self.delete_selected())
        m.exec(self.table.viewport().mapToGlobal(pos))

    def clear(self):
        self.entries.clear()
        self.fingerprints.clear()
        self.table.setRowCount(0)
        self._load(None)
        self._show_opts([])

    def _selection_changed(self):
        sel = self.selected_entries()
        self._load(sel[0] if len(sel) == 1 else None)
        self._show_opts(sel)
        self._show_name(sel)
        n = len(sel)
        self.b_queue_sel.setText(f"Add {n} selected to queue" if n > 1 else "Add to queue" if n else "Add selected to queue")
        self.b_queue_sel.setEnabled(n > 0)
        self.estimate_label.setText("")

    def _default_stem(self, e):
        return e.path.stem + self.main.settings.name_suffix

    def _show_name(self, sel):
        one = sel[0] if len(sel) == 1 else None
        self.out_name.setEnabled(one is not None)
        if one is None:
            self.out_name.setText("")
            self.out_name.setPlaceholderText("Select one clip to rename its output" if sel else "")
            return
        prefill = self.main.settings.prefill_names
        self.out_name.setPlaceholderText(self._default_stem(one) + "  (original name)")
        self.out_name.setText(one.out_name or (self._default_stem(one) if prefill else ""))
        self.out_name.end(False)        # cursor at the end, ready to add to the name

    def _rename(self, text):
        sel = self.selected_entries()
        if len(sel) != 1:
            return
        e = sel[0]
        text = text.strip()
        # The unchanged original name isn't a rename (so suffix and tags still apply).
        e.out_name = sanitize_filename(text) if text and text != self._default_stem(e) else ""
        self._fill_row(self.entries.index(e), e)

    # ------------------------------------------------------ per-clip options
    def _eff(self, e) -> dict:
        """The options a clip is saved with: its own, else the shared ones."""
        return e.opts or self.shared

    def _show_opts(self, sel):
        """Show the first selected clip's options, or the shared ones when nothing is selected."""
        if not sel:
            self.opts_title.setText("Options for all clips")
        elif len(sel) == 1:
            self.opts_title.setText(f"Options for {sel[0].path.name}")
        else:
            self.opts_title.setText(f"Options for {len(sel)} selected clips")
        st = self._eff(sel[0]) if sel else self.shared
        self._build_track_checks(sel or self.entries, st)
        self.form.write(st)
        self.b_shared.setEnabled(any(e.opts is not None for e in sel))

    def _change(self, fn):
        """Apply a change: to the shared options, or to each selected clip's own (starting from what
        it uses now)."""
        sel = self.selected_entries()
        if not sel:
            fn(self.shared)
            return
        for e in sel:
            if e.opts is None:
                e.opts = copy_opts(self.shared)
            fn(e.opts)
            self._fill_row(self.entries.index(e), e)
        self.b_shared.setEnabled(True)

    def _opt_changed(self, key, value):
        self._change(lambda st: st.__setitem__(key, value))

    def _track_toggled(self, key, t, on):
        if self.form.writing:
            return
        add = on if key == "rip_tracks" else not on

        def fn(st):
            got = set(st[key])
            (got.add if add else got.discard)(t)
            st[key] = sorted(got)
        self._change(fn)

    def use_shared(self):
        """The selected clips drop their own options."""
        sel = self.selected_entries()
        for e in sel:
            e.opts = None
            self._fill_row(self.entries.index(e), e)
        self._show_opts(sel)

    def deselect_all(self):
        self.table.selectionModel().clear()

    def _build_track_checks(self, entries, st):
        """One keep and one rip checkbox per audio track of these clips, ticked as `st` says."""
        for lay in (self.track_lay, self.rip_lay):
            while lay.count():
                w = lay.takeAt(0).widget()
                if w:
                    w.deleteLater()
        n = max((len(e.info.audio) for e in entries), default=0)
        ref = max(entries, key=lambda e: len(e.info.audio)).info.audio if entries else []
        self.keep_checks, self.rip_checks = [], []
        for t in range(n):
            label = f"{t + 1}: {ref[t].label}" if t < len(ref) else f"Track {t + 1}"
            k = QCheckBox(label)
            k.setChecked(t not in st["drop_tracks"])
            k.toggled.connect(lambda on, t=t: self._track_toggled("drop_tracks", t, on))
            self.track_lay.addWidget(k)
            self.keep_checks.append(k)
            rc = QCheckBox(label)
            rc.setChecked(t in st["rip_tracks"])
            rc.toggled.connect(lambda on, t=t: self._track_toggled("rip_tracks", t, on))
            self.rip_lay.addWidget(rc)
            self.rip_checks.append(rc)
        if n == 0:
            self.track_lay.addWidget(QLabel("(no audio)"))
            self.rip_lay.addWidget(QLabel("(no audio)"))
        self.track_lay.addStretch(1)
        self.rip_lay.addStretch(1)

    # ------------------------------------------------------------- preview
    def _load(self, e):
        self.current = e
        self.player.stop()
        if e is None:
            self.player.setSource(QUrl())
            self.timeline.set_state(duration=0, position=0, segments=[], pending_in=None, snaps=[])
            self.timeline.set_peek([], [])
            self.seg_list.clear()
            self.trim_info.setText("Select one clip to preview and trim it." if self.entries else "")
            return
        self.player.setSource(QUrl.fromLocalFile(str(e.path.resolve())))
        self.timeline.set_state(duration=e.info.duration, position=0, segments=e.segments,
                                pending_in=None, snaps=[])
        self.timeline.set_peek([], [])
        self._load_peek(e)
        self._refresh_segments()

    def _load_peek(self, e):
        """Waveform and thumbnails for the timeline, computed off the GUI thread."""
        if not self.main.settings.show_peek or not e.info.duration:
            return
        count = max(4, min(60, self.timeline.width() // 80))
        if e.info.audio:
            t = Task(lambda: peek.waveform(e.path), parent=self)
            t.done.connect(lambda w, e=e: e is self.current and self.timeline.set_peek(waveform=w))
            self.tasks.append(t)
            t.start()
        if e.info.has_video:
            t = Task(lambda: peek.thumbnails(e.path, e.info.duration, count), parent=self)
            t.done.connect(lambda th, e=e: e is self.current and self.timeline.set_peek(thumbs=th))
            self.tasks.append(t)
            t.start()

    def _tracks_changed(self):
        tracks = self.player.audioTracks()
        info = self.current.info.audio if self.current else []
        labels = [info[i].label if i < len(info) else f"Track {i + 1}" for i in range(len(tracks))]
        titles = [(info[i].title or info[i].label) if i < len(info) else labels[i] for i in range(len(tracks))]
        self.preview_track.set_tracks(labels, titles)

    def _pos_changed(self, ms):
        t = ms / 1000
        self.timeline.set_state(position=t)
        self.time_label.setText(f"{format_hms_ms(t)}  /  {format_hms_ms(self.timeline.duration)}")

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.b_play.setText("Play")
        else:
            self.player.play()
            self.b_play.setText("Pause")

    def step(self, frames):
        if not self.current:
            return
        self.player.pause()
        self.b_play.setText("Play")
        fps = self.current.info.fps or 30
        self.player.setPosition(max(0, int(self.player.position() + frames * 1000 / fps)))

    def jump(self, seconds):
        self.player.setPosition(max(0, int(self.player.position() + seconds * 1000)))

    def _now(self):
        return self.player.position() / 1000

    def mark_in(self):
        if self.current:
            self.timeline.set_state(pending_in=self._now())
            self.trim_info.setText(f"In at {format_hms_ms(self._now())}. Now move to the end of the part and press O.")

    def mark_out(self):
        e = self.current
        if not e:
            return
        out = self._now()
        start = self.timeline.pending_in
        if start is None:
            start = max([b for _, b in e.segments if b is not None and b <= out] or [0.0])
        if out - start < 0.1:
            self.trim_info.setText("The out point must be after the in point.")
            return
        segs = sorted(e.segments + [(start, out)])
        merged = []
        for a, b in segs:            # merge overlapping parts
            if merged and a <= merged[-1][1] + 0.01:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        e.segments = merged
        self.timeline.set_state(segments=merged, pending_in=None)
        self._refresh_segments()

    def remove_segment(self):
        e = self.current
        r = self.seg_list.currentRow()
        if e and 0 <= r < len(e.segments):
            e.segments.pop(r)
            self.timeline.set_state(segments=e.segments)
            self._refresh_segments()

    def clear_segments(self):
        if self.current:
            self.current.segments = []
            self.timeline.set_state(segments=[], pending_in=None, snaps=[])
            self._refresh_segments()

    def _refresh_segments(self):
        e = self.current
        self.seg_list.clear()
        if not e:
            return
        for a, b in e.segments:
            self.seg_list.addItem(f"{format_hms_ms(a)}  to  {format_hms_ms(b)}   ({b - a:.1f}s)")
        r = self.entries.index(e)
        self._fill_row(r, e)
        self._update_info()
        if e.segments:
            starts = [a for a, _ in e.segments]
            t = Task(lambda: [ff.keyframe_at_or_before(e.path, s) for s in starts], parent=self)
            t.done.connect(lambda kfs, e=e: self._show_snaps(e, kfs))
            self.tasks.append(t)
            t.start()

    def _show_snaps(self, e, kfs):
        if e is not self.current:
            return
        diffs = [(a, k) for (a, _), k in zip(e.segments, kfs) if k is not None and a - k > 0.01]
        e.snap_note = ""
        if diffs:
            worst = max(a - k for a, k in diffs)
            e.snap_note = (f"Without re-encoding, cuts start on the nearest keyframe before your mark "
                           f"(up to {worst:.2f}s earlier), so nothing you marked is lost; audio rips start there too. "
                           f"Tick 'Frame-exact cuts' or 'Compress' for exact cuts.")
        e.snap_marks = [k for _, k in diffs]
        self._update_info()

    def _update_info(self):
        e = self.current
        if not e:
            return
        if not e.segments:
            self.trim_info.setText("Keeping the whole clip. Mark in/out to keep only part of it; "
                                   "add several parts to cut out the middle.")
            return
        txt = f"Keeping {len(e.segments)} part(s), {self._kept(e):.1f}s of {e.info.duration:.1f}s."
        snapping = not (self.compress.isChecked() or self.exact.isChecked())
        # Keyframe markers only matter when cuts really snap to them.
        self.timeline.set_state(snaps=e.snap_marks if snapping else [])
        if e.snap_note and snapping:
            txt += " " + e.snap_note
        self.trim_info.setText(txt)

    # --------------------------------------------------------------- jobs
    def _options(self, st) -> JobOptions:
        o = JobOptions.from_settings(self.main.settings)
        o.output_dir = self.main.output_dir()
        o.exact_cuts = st["exact"]
        o.delete_originals = st["delete_orig"]
        o.rip_format = st["rip_fmt"]
        return o

    def build_jobs(self, entries):
        """One job per clip, each with its own options or the shared ones."""
        plans = [(e, self._eff(e)) for e in entries]
        for e, st in plans:
            problems = self.encode_settings(st["preset"]).problems() if st["save_video"] and st["compress"] else []
            if problems:
                QMessageBox.warning(self, "Compression settings", "\n".join(problems))
                return []
        idle = [e for e, st in plans if not st["save_video"] and not st["rip_video"]
                and not [t for t in st["rip_tracks"] if t < len(e.info.audio)]]
        if idle:
            QMessageBox.information(self, "Nothing to do",
                                    f"Nothing to save for {', '.join(e.path.name for e in idle[:3])}"
                                    f"{' and more' if len(idle) > 3 else ''}. "
                                    "Tick 'Save the edited video' or choose tracks to rip.")
            return []
        jobs = []
        for e, st in plans:
            n = len(e.info.audio)
            tracks = [t for t in range(n) if t not in st["drop_tracks"]]
            job = ClipJob(
                source=str(e.path), segments=list(e.segments), make_video=st["save_video"],
                audio_tracks=None if len(tracks) == n else tracks,
                mix_tracks=tracks if (st["mix"] and len(tracks) >= 2) else None,
                keep_mixed_originals=st["mix_keep"],
                compress=st["save_video"] and st["compress"], encode=self.encode_settings(st["preset"]).to_dict(),
                rip_audio_tracks=[t for t in st["rip_tracks"] if t < n], rip_video=st["rip_video"],
                output_name=e.out_name,
                options=self._options(st))
            jobs.append(job)
        return jobs

    def queue(self, selected_only=True):
        entries = self.selected_entries() if selected_only else list(self.entries)
        if not entries:
            return
        n = sum(1 for e in entries if self._eff(e)["delete_orig"])
        if n:
            where = "permanently deleted" if self.main.settings.permanent_delete else "moved to the Recycle Bin"
            if QMessageBox.question(self, "Delete originals?",
                                    f"The {n} original file(s) will be {where} after their "
                                    f"outputs are saved and checked. Continue?") != QMessageBox.Yes:
                return
        jobs = self.build_jobs(entries)
        if jobs:
            jobs = check_new_jobs(self.main, jobs, {str(e.path): e.info for e in entries}, self)
        if jobs:
            self.queue_jobs.emit(jobs)

    def estimate(self):
        entries = self.selected_entries() or list(self.entries)
        if not entries:
            return
        plans = []
        for e in entries:
            st = self._eff(e)
            plans.append((e, st["save_video"] and st["compress"], self.encode_settings(st["preset"])))
        lossless = any(compress and enc.mode == "lossless" for _, compress, enc in plans)

        def work():
            total_in = total_out = 0
            for e, compress, enc in plans:
                kept = self._kept(e)
                part_in = int(e.info.size * kept / e.info.duration) if e.info.duration else e.info.size
                total_in += e.info.size
                if not compress:
                    total_out += part_in
                elif enc.mode == "target_size":
                    total_out += int(enc.target_size_mb * 1024 * 1024)
                elif enc.mode == "lossless":
                    total_out += int(part_in * 2)      # rough: lossless is usually larger than OBS output
                else:
                    q, _ = resolve_quality(e.path, e.info, enc)
                    total_out += predict_size(e.path, e.info, enc, q, kept)
            return total_in, total_out

        self.estimate_label.setText("Estimating…")
        t = Task(work, parent=self)
        t.done.connect(lambda r: self.estimate_label.setText(
            f"About {format_size(r[1])} from {format_size(r[0])} of originals"
            + (f" (saves about {format_size(r[0] - r[1])})" if r[0] > r[1] else "")
            + (". Lossless is a rough guess." if lossless else ".")))
        t.failed.connect(lambda m: self.estimate_label.setText(f"Couldn't estimate: {m}"))
        self.tasks.append(t)
        t.start()
