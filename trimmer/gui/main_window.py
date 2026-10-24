"""Main window: the Clips / Replay merge / Queue tabs, drag and drop,
the toolbar and start-up checks."""

import shutil
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QFileDialog, QLabel, QMainWindow, QMessageBox,
                               QSizePolicy, QSystemTrayIcon, QTabWidget, QToolBar, QWidget)

from ..core import ffmpeg as ff
from ..core import jobs as J
from ..core.journal import clear_queue, load_queue, save_queue
from ..core.settings import AppSettings
from ..core.util import VIDEO_EXTENSIONS, find_video_files
from .clips_tab import ClipsTab
from .merge_tab import PREVIEW_PREFIX, MergeTab
from .queue_tab import QueueTab
from .settings_dialog import SettingsDialog, TipsDialog
from .widgets import apply_theme, label
from .worker import Task

HW_TEST = {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "amf": "h264_amf"}


def detect_encoders():
    """Encoder families that really work on this PC (a one-frame test each)."""
    found = ["software"]
    for key, name in HW_TEST.items():
        if ff.encoder_works(name):
            found.append(key)
    return found


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings = None):
        super().__init__()
        self.settings = settings or AppSettings.load()
        self.encoders = ["software"]
        self.track_prefs = {}           # default preview track, for this run only
        self.tasks = []
        ff.set_ffmpeg_dir(self.settings.ffmpeg_dir or None)
        self.setWindowTitle("Trimmer")
        self.resize(1280, 820)
        self.setAcceptDrops(True)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setDrawBase(False)
        self.clips = ClipsTab(self)
        self.merge = MergeTab(self)
        self.queue = QueueTab(self)
        self.tabs.addTab(self.clips, "Clips")
        self.tabs.addTab(self.merge, "Replay merge")
        self.tabs.addTab(self.queue, "Queue")
        self.setCentralWidget(self.tabs)
        for tab in (self.clips, self.merge):
            tab.queue_jobs.connect(self._queue_jobs)
        for tab in (self.clips, self.merge, self.queue):
            tab.status.connect(self.show_status)
        self.queue.running_changed.connect(self._running)

        self._toolbar()
        self.statusBar().setSizeGripEnabled(False)
        self.out_label = QLabel()
        self.statusBar().addPermanentWidget(self.out_label)
        self._show_output()
        self.show_status("Drop clips or folders anywhere in this window.")

    # -------------------------------------------------------------- toolbar
    def _toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setContextMenuPolicy(Qt.PreventContextMenu)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)
        tb.addWidget(label("Trimmer", "brand"))

        def act(text, fn, key=None, tip=None):
            a = QAction(text, self)
            a.triggered.connect(fn)
            if key:
                a.setShortcut(QKeySequence(key))
            a.setToolTip(tip or text)
            tb.addAction(a)
            return a

        act("Add files", self.add_files_dialog, "Ctrl+O")
        act("Add folder", self.add_folder_dialog, "Ctrl+Shift+O")
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        act("Output folder", self.pick_output, tip="Where finished files are saved")
        act("OBS tips", lambda: TipsDialog(self).exec())
        act("Settings", self.open_settings, "Ctrl+,")

    def output_dir(self) -> str:
        return self.settings.output_dir

    def _show_output(self):
        d = self.settings.output_dir
        self.out_label.setText("Saving to: " + (d if d else "same folder as each original"))

    def pick_output(self):
        d = QFileDialog.getExistingDirectory(self, "Choose the output folder", self.settings.output_dir or "")
        if d:
            self.settings.output_dir = d
            self.settings.save()
            self._show_output()

    def show_status(self, text: str):
        self.statusBar().showMessage(text, 15000)

    # --------------------------------------------------------------- adding
    def _route(self, paths):
        paths = [Path(p) for p in paths]
        if not paths:
            return
        target = self.merge if self.tabs.currentWidget() is self.merge else self.clips
        if target is self.clips:
            files = find_video_files(paths, recursive=True)
            if not files:
                self.show_status("No video files found in what was dropped.")
                return
            self.tabs.setCurrentWidget(self.clips)
            self.clips.add_paths(files)
        else:
            self.merge.add_paths(find_video_files(paths, recursive=True))

    def add_files_dialog(self):
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS))
        files, _ = QFileDialog.getOpenFileNames(self, "Add clips", self.settings.ui.get("last_dir", ""),
                                                f"Videos ({exts});;All files (*)")
        if files:
            self.settings.ui["last_dir"] = str(Path(files[0]).parent)
            self._route(files)

    def add_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "Add a folder of clips", self.settings.ui.get("last_dir", ""))
        if d:
            self.settings.ui["last_dir"] = d
            self._route([d])

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        e.acceptProposedAction()
        self._route(paths)

    # ---------------------------------------------------------------- queue
    def _queue_jobs(self, jobs):
        if jobs:
            self.queue.add_jobs(jobs, start=True)
            if self.queue.auto_starts():
                self.tabs.setCurrentWidget(self.queue)
            self._running(self.queue.is_running())

    def _running(self, running):
        waiting = sum(1 for i in self.queue.items if i.status == "queued")
        if running:
            text = "Queue  •  paused" if self.queue.paused else "Queue  •  working"
        else:
            text = f"Queue  •  {waiting} waiting" if waiting else "Queue"
        self.tabs.setTabText(2, text)

    def notify_done(self, summary):
        QApplication.alert(self)
        if QSystemTrayIcon.isSystemTrayAvailable() and not self.isActiveWindow():
            tray = getattr(self, "_tray", None)
            if tray is None:
                self._tray = tray = QSystemTrayIcon(self.windowIcon(), self)
            tray.show()
            tray.showMessage("Trimmer", "Queue finished. " + summary)

    # ------------------------------------------------------------- settings
    def open_settings(self):
        old_theme, old_ff = self.settings.theme, self.settings.ffmpeg_dir
        accepted = SettingsDialog(self.settings, self.encoders, self).exec()
        self.queue.refresh_savings()      # the Reset button may have changed it
        if accepted:
            if self.settings.ffmpeg_dir != old_ff:
                ff.set_ffmpeg_dir(self.settings.ffmpeg_dir or None)
                self.startup_checks()
            if self.settings.theme != old_theme:
                apply_theme(QApplication.instance(), self.settings.theme)
                for w in QApplication.instance().allWidgets():
                    w.update()
            self.clips.refresh_from_settings()
            self.merge.refresh_from_settings()
            self._show_output()

    # -------------------------------------------------------------- startup
    def startup_checks(self):
        """Run after the window is shown: find ffmpeg, test encoders, offer
        to restore an unfinished queue."""
        for old in Path(tempfile.gettempdir()).glob(PREVIEW_PREFIX + "*"):   # left by a crash
            try:
                if time.time() - old.stat().st_mtime > 6 * 3600:     # not another window's open preview
                    shutil.rmtree(old, ignore_errors=True)
            except OSError:
                pass
        if ff.ffmpeg_version() == "not found":
            QMessageBox.warning(
                self, "ffmpeg not found",
                "Trimmer needs ffmpeg and ffprobe. Install ffmpeg and add it to PATH, or set its "
                "folder in Settings → General.")
            return

        def found(enc):
            self.encoders = enc
            hw = [e for e in enc if e != "software"]
            self.show_status("Hardware encoding available: " + ", ".join(hw) if hw
                             else "No hardware encoder found; using the CPU.")

        t = Task(detect_encoders, parent=self)
        t.done.connect(found)
        self.tasks.append(t)
        t.start()
        self._restore_queue()

    def _restore_queue(self):
        saved = load_queue()
        if not saved or self.queue.items:
            return
        jobs = []
        for d in saved:
            try:
                jobs.append(J.job_from_dict(d))
            except (TypeError, ValueError, KeyError):
                continue
        jobs = [j for j in jobs if all(Path(p).exists() for p in j.sources())]
        if not jobs:
            clear_queue()
            return
        ans = QMessageBox.question(
            self, "Unfinished queue",
            f"{len(jobs)} job(s) were still waiting when Trimmer last closed. Add them back to the queue?",
            QMessageBox.Yes | QMessageBox.No)
        if ans == QMessageBox.Yes:
            self.queue.add_jobs(jobs, start=False)
            self.tabs.setCurrentWidget(self.queue)
        else:
            clear_queue()

    def closeEvent(self, e):
        if self.queue.is_running():
            ans = QMessageBox.question(
                self, "Still working",
                "A job is running. Stop it and quit? The partial file will be removed and waiting jobs "
                "will be offered again next time.")
            if ans != QMessageBox.Yes:
                e.ignore()
                return
            pending = [J.job_to_dict(i.job) for i in self.queue.items if i.status in ("queued", "running")]
            self.queue.closing = True
            self.queue.stop_all()
            self.queue.runner.wait(15000)
            save_queue(pending)
        self.merge.watcher.stop()
        # Background look-ups (keyframes, waveform, thumbnails, probes) finish
        # in a moment; a thread destroyed while running can crash on exit.
        for t in self.tasks + self.clips.tasks + self.merge.tasks + list(self.merge.watcher._checking.values()):
            if t.isRunning():
                t.wait(3000)
        self.clips.player.stop()
        self.settings.save()
        e.accept()
