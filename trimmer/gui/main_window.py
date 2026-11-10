"""Main window: the Clips / Replay merge / Queue tabs, drag and drop,
the toolbar and start-up checks."""

import json
import shutil
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QLabel, QMainWindow, QMessageBox,
                               QMenu, QSizePolicy, QSystemTrayIcon, QTabWidget, QToolBar, QToolButton,
                               QWidget)

from ..core import ffmpeg as ff
from ..core import jobs as J
from ..core.journal import clean_logs, clear_queue, load_queue, save_queue
from ..core.session import autosave_path, clear_session, load_session, save_session
from ..core.settings import AppSettings
from ..core.util import VIDEO_EXTENSIONS, find_video_files, recycle_or_delete, same_file
from .clips_tab import ClipsTab
from .merge_tab import PREVIEW_PREFIX, MergeTab
from .queue_tab import QueueTab
from .help import GETTING_STARTED, SHORTCUTS, HelpDialog
from .settings_dialog import SettingsDialog, TipsDialog
from .system_hooks import QueueGuard
from .updates import Updater, open_releases_page
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


def ask_close_choice(parent):
    """Ask what to do with the open clips, replays and jobs. Returns
    ("save" | "discard" | "cancel", remember_my_choice)."""
    box = QMessageBox(QMessageBox.Question, "Save before closing?",
                      "There are clips, replays or jobs still open. Save them so they are back "
                      "the next time Trimmer starts?", parent=parent)
    b_save = box.addButton("Save and close", QMessageBox.AcceptRole)
    b_discard = box.addButton("Close without saving", QMessageBox.DestructiveRole)
    box.addButton(QMessageBox.Cancel)
    box.setDefaultButton(b_save)
    remember = QCheckBox("Remember my choice (change it later in Settings)")
    box.setCheckBox(remember)
    box.exec()
    clicked = box.clickedButton()
    choice = "save" if clicked is b_save else "discard" if clicked is b_discard else "cancel"
    return choice, remember.isChecked()


class MainWindow(QMainWindow):
    AUTOSAVE_MS = 10000          # how often what's open is saved in case of a crash (0: never)

    def __init__(self, settings: AppSettings = None):
        super().__init__()
        self.settings = settings or AppSettings.load()
        self.encoders = ["software"]
        self.track_prefs = {}           # default preview track, for this run only
        self.tasks = []
        self.updater = Updater(self)    # does nothing unless the user said yes to update checks
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
        self.tabs.currentChanged.connect(lambda _: self.statusBar().clearMessage())   # no stale messages
        self.tabs.setTabVisible(self.tabs.indexOf(self.merge), self.settings.show_replay_merge)
        for tab in (self.clips, self.merge):
            tab.queue_jobs.connect(self._queue_jobs)
        for tab in (self.clips, self.merge, self.queue):
            tab.status.connect(self.show_status)
        self.queue.running_changed.connect(self._running)
        self.queue.jobs_changed.connect(self.clips.refresh_queue_notes)
        self.queue.jobs_changed.connect(self.merge.refresh_queue_notes)

        self._toolbar()
        self.statusBar().setSizeGripEnabled(False)
        self.out_label = QLabel()
        self.statusBar().addPermanentWidget(self.out_label)
        self._show_output()
        self.show_status("Drop clips or folders anywhere in this window.")
        self.guard = QueueGuard(self)   # game auto-pause and the global pause key
        problem = self.guard.apply_settings()
        if problem:
            self.show_status(problem)
        self._autosaved = None          # the last autosave written, to skip unchanged ones
        self.autosave_timer = QTimer(self)
        self.autosave_timer.timeout.connect(self.autosave)

    # -------------------------------------------------------------- toolbar
    def _toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setContextMenuPolicy(Qt.PreventContextMenu)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)
        logo = Path(__file__).resolve().parent / "assets" / "trimmer.png"
        if logo.exists():
            mark = QLabel()
            mark.setPixmap(QIcon(str(logo)).pixmap(22, 22))
            mark.setContentsMargins(10, 0, 2, 0)
            tb.addWidget(mark)
        tb.addWidget(label("Trimmer", "brand"))

        def act(text, fn, key=None, tip=None):
            a = QAction(text, self)
            a.triggered.connect(fn)
            if key:
                a.setShortcut(QKeySequence(key))
            a.setToolTip(tip or text)
            tb.addAction(a)
            return a

        act("Add files", self.add_files_dialog, "Ctrl+O", "Add clips to the tab you're on (or drop them anywhere)")
        act("Add folder", self.add_folder_dialog, "Ctrl+Shift+O", "Add every clip in a folder to the tab you're on")
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        act("Output folder…", self.pick_output, tip="Choose where finished files are saved")
        help_menu = QMenu(self)
        a = help_menu.addAction("Getting started", self.show_getting_started)
        a.setShortcut(QKeySequence("F1"))
        self.addAction(a)                      # F1 works without opening the menu
        help_menu.addAction("Keyboard shortcuts", lambda: HelpDialog("Keyboard shortcuts", SHORTCUTS, self,
                                                                      (560, 640)).exec())
        help_menu.addAction("OBS settings tips", lambda: TipsDialog(self).exec())
        help_menu.addSeparator()
        if self.updater.available():
            help_menu.addAction("Check for updates", lambda: self.updater.check(manual=True))
        else:                                  # local features only: the browser fetches it, not Trimmer
            help_menu.addAction("Get the latest version", open_releases_page)
        help_menu.addAction("About Trimmer", lambda: self.open_settings(tab="About"))
        b_help = QToolButton()
        b_help.setText("Help")
        b_help.setMenu(help_menu)
        b_help.setPopupMode(QToolButton.InstantPopup)
        b_help.setToolButtonStyle(Qt.ToolButtonTextOnly)
        b_help.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")
        tb.addWidget(b_help)
        act("Settings", lambda: self.open_settings(), "Ctrl+,")

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

    def send_to_clips(self, paths):
        """Add files to the Clips tab and show it (used by Replay merge's right-click menu)."""
        self.tabs.setCurrentWidget(self.clips)
        self.clips.add_paths([Path(p) for p in paths])

    def release_files(self, paths):
        """Close every preview of these files so they can be deleted."""
        self.clips.release(paths)
        self.merge.release(paths)

    def delete_originals(self, paths, parent=None) -> list:
        """Move files to the Recycle Bin (or delete them, as Settings say) after asking. Files a waiting or
        running job still needs are left alone. Returns the paths that are really gone."""
        parent = parent or self
        paths = list(dict.fromkeys(Path(p) for p in paths))
        busy = [p for p in paths if any(same_file(p, src) for i in self.queue.items
                                        if i.status in ("queued", "running") for src in i.job.sources())]
        paths = [p for p in paths if p not in busy]
        if busy:
            QMessageBox.information(
                parent, "In the queue",
                f"{len(busy)} file(s) are still waiting or being saved in the queue and were left alone:\n\n"
                + "\n".join(p.name for p in busy[:8]) + (f"\n… and {len(busy) - 8} more" if len(busy) > 8 else ""))
        if not paths:
            return []
        permanent = self.settings.permanent_delete
        names = "\n".join(p.name for p in paths[:8]) + (f"\n… and {len(paths) - 8} more" if len(paths) > 8 else "")
        if permanent:
            title, text = "Delete permanently?", (f"Permanently delete {len(paths)} file(s)? "
                                                  f"This can't be undone.\n\n{names}")
        else:
            title, text = "Move to Recycle Bin?", f"Move {len(paths)} file(s) to the Recycle Bin?\n\n{names}"
        if QMessageBox.question(parent, title, text, QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return []
        gone, errors = [], []
        for p in paths:
            try:
                recycle_or_delete(p, permanent=permanent)
                gone.append(p)
            except OSError as e:
                errors.append(f"{p.name}: {e}")
        if errors:
            QMessageBox.warning(parent, "Some files weren't removed",
                                f"{len(errors)} file(s) couldn't be removed and stay in the list:\n\n"
                                + "\n".join(errors[:8]))
        if gone:
            self.show_status(f"{len(gone)} file(s) " + ("deleted." if permanent else "moved to the Recycle Bin."))
        return gone

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
    def show_getting_started(self):
        HelpDialog("Getting started", GETTING_STARTED, self).exec()

    def open_settings(self, tab: str = None):
        old_theme, old_ff = self.settings.theme, self.settings.ffmpeg_dir
        dialog = SettingsDialog(self.settings, self.encoders, self)
        if tab:
            dialog.show_tab(tab)
        accepted = dialog.exec()
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
            self._show_merge_tab(self.settings.show_replay_merge)
            problem = self.guard.apply_settings()
            if problem:
                QMessageBox.warning(self, "Global pause key", problem)
            self._show_output()

    def _show_merge_tab(self, shown: bool):
        i = self.tabs.indexOf(self.merge)
        if self.tabs.isTabVisible(i) == shown:
            return
        if not shown and self.tabs.currentWidget() is self.merge:
            self.tabs.setCurrentWidget(self.clips)
        self.tabs.setTabVisible(i, shown)
        self.merge.set_shown(shown)

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
        if self.settings.auto_delete_logs and self.settings.keep_logs_days > 0:
            clean_logs(self.settings.keep_logs_days)
        if ff.ffmpeg_version() == "not found" and not self.find_ffmpeg():
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
        if not self.restore_session() and not self.restore_autosave():
            self._restore_queue()
        if self.AUTOSAVE_MS:
            self.autosave_timer.start(self.AUTOSAVE_MS)

    FFMPEG_HELP = (
        "Trimmer does its video work with ffmpeg, a free tool that isn't installed yet.\n\n"
        "To get it, do one of these:\n"
        "•  In a terminal, run:  winget install Gyan.FFmpeg  and then restart Trimmer.\n"
        "•  Download a Windows build of ffmpeg from ffmpeg.org (\"essentials\" or \"full\"), unzip it, "
        "and either pick its bin folder below or put it in a folder named \"ffmpeg\" next to Trimmer.\n\n"
        "A \"full\" build also enables the measured auto quality (auto3) with VMAF.")

    def find_ffmpeg(self) -> bool:
        """First launch without ffmpeg: explain how to get it and let the user
        pick its folder. True once ffmpeg is found."""
        while True:
            box = QMessageBox(QMessageBox.Warning, "ffmpeg is needed", self.FFMPEG_HELP, parent=self)
            pick = box.addButton("Choose ffmpeg folder…", QMessageBox.AcceptRole)
            box.addButton("Later", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is not pick:
                self.show_status("ffmpeg not found: set its folder in Settings → App.")
                return False
            folder = QFileDialog.getExistingDirectory(self, "Folder with ffmpeg.exe and ffprobe.exe")
            if not folder:
                continue
            ff.set_ffmpeg_dir(folder)
            if ff.ffmpeg_version() != "not found" and not ff.tools_available():
                self.settings.ffmpeg_dir = folder
                self.settings.save()
                self.show_status(f"Using ffmpeg from {folder}")
                return True
            ff.set_ffmpeg_dir(self.settings.ffmpeg_dir or None)
            QMessageBox.warning(self, "ffmpeg not found",
                                "That folder doesn't have both ffmpeg.exe and ffprobe.exe in it. "
                                "Pick the bin folder inside the unzipped ffmpeg download.")

    # ----------------------------------------------------------- save state
    def restore_session(self) -> bool:
        """Bring back what was saved on the last clean close. One-shot: the
        file is deleted once it has been read."""
        state = load_session()
        if state is None:
            return False
        clear_session()
        clear_session(autosave_path())      # a clean close leaves none; never offer both
        self._restore_state(state)
        return True

    def _state(self) -> dict:
        return {"clips": self.clips.snapshot(), "merge": self.merge.snapshot()}

    def autosave(self):
        """Save what's open in Clips and Replay merge, so a crash or power cut doesn't lose it. The
        queue saves itself (queue.json). Nothing open: no file."""
        state = self._state()
        if not (state["clips"]["entries"] or state["merge"]["paths"]):
            if self._autosaved is not None or autosave_path().exists():
                clear_session(autosave_path())
                self._autosaved = None
            return
        text = json.dumps(state, sort_keys=True)
        if text != self._autosaved and save_session(state, autosave_path()):
            self._autosaved = text

    def restore_autosave(self) -> bool:
        """After a crash: offer what was open, along with the jobs still waiting in the queue."""
        state = load_session(autosave_path())
        if state is None:
            return False
        clips = state.get("clips") if isinstance(state.get("clips"), dict) else {}
        merge = state.get("merge") if isinstance(state.get("merge"), dict) else {}
        n_clips = len(clips.get("entries") or [])
        n_reps = len(merge.get("paths") or [])
        if not (n_clips or n_reps):
            clear_session(autosave_path())
            return False
        queue = load_queue()
        what = [f"{n} {noun}{'s' if n != 1 else ''}" for n, noun in
                ((n_clips, "clip"), (n_reps, "replay"), (len(queue), "waiting job")) if n]
        ans = QMessageBox.question(
            self, "Trimmer didn't close properly",
            "Trimmer or the PC seems to have stopped without closing Trimmer first. Bring back what was "
            f"open ({', '.join(what)}) with your trims, names and options?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        clear_session(autosave_path())
        if ans != QMessageBox.Yes:
            clear_queue()
            return True
        self._restore_state({**state, "queue": queue}, start=False)
        return True

    def _restore_state(self, state: dict, start=True):
        missing = []
        clips, merge = state.get("clips"), state.get("merge")
        if isinstance(clips, dict):
            missing += self.clips.restore(clips)
        if isinstance(merge, dict):
            missing += self.merge.restore(merge)
        jobs = []
        for d in state.get("queue") or []:
            try:
                jobs.append(J.job_from_dict(d))
            except (TypeError, ValueError, KeyError):
                continue
        for j in jobs:
            missing += [p for p in j.sources() if not Path(p).exists()]
        jobs = [j for j in jobs if all(Path(p).exists() for p in j.sources())]
        clear_queue()
        if jobs:
            self.queue.add_jobs(jobs, start=start)
        self._running(self.queue.is_running())
        if clips and clips.get("entries"):
            self.tabs.setCurrentWidget(self.clips)
        if missing:
            names = list(dict.fromkeys(missing))
            QMessageBox.information(
                self, "Some files are gone",
                f"{len(names)} saved file(s) no longer exist and were skipped:\n\n"
                + "\n".join(names[:12]) + (f"\n… and {len(names) - 12} more" if len(names) > 12 else ""))

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

    def _close_choice(self, has_state: bool):
        """save | discard | cancel, from the On close setting or by asking."""
        if not has_state:
            return "discard"
        mode = self.settings.on_close
        if mode in ("save", "discard"):
            return mode
        choice, remember = ask_close_choice(self)
        if remember and choice in ("save", "discard"):
            self.settings.on_close = choice
        return choice

    def closeEvent(self, e):
        running = self.queue.is_running()
        if running:
            ans = QMessageBox.question(
                self, "Still working",
                "A job is running. Stop it and quit? The partial file will be removed and waiting jobs "
                "will be offered again next time.")
            if ans != QMessageBox.Yes:
                e.ignore()
                return
        # A running job that gets stopped counts as unfinished.
        pending = [J.job_to_dict(i.job) for i in self.queue.items if i.status in ("queued", "running")]
        choice = self._close_choice(bool(self.clips.entries or self.merge.paths or pending))
        if choice == "cancel":
            e.ignore()
            return
        if running:
            self.queue.closing = True
            self.queue.stop_all()
            self.queue.runner.wait(15000)
            save_queue(pending)
        self.autosave_timer.stop()
        clear_session(autosave_path())      # a clean close: nothing to recover next time
        if choice == "save":
            self.queue.closing = True       # the jobs live in the saved state now
            saved = save_session({**self._state(), "queue": pending})
            if saved:
                clear_queue()
        else:
            clear_session()
            self.queue.closing = True
            clear_queue()
        self.merge.watcher.stop()
        self.queue.stop_estimates(wait_ms=5000)
        self.guard.stop()
        # Background look-ups (keyframes, waveform, thumbnails, probes) finish
        # in a moment; a thread destroyed while running can crash on exit.
        for t in self.tasks + self.clips.tasks + self.merge.tasks + list(self.merge.watcher._checking.values()):
            if t.isRunning():
                t.wait(3000)
        self.clips.player.stop()
        # Anything still running (a look-up that didn't finish in time, a
        # quality trial) is ended now, so no ffmpeg keeps going after Trimmer.
        ff.kill_all()
        self.settings.save()
        e.accept()
