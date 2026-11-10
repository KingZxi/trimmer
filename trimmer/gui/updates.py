"""The update popup, the first-launch question and Install update. All network work goes through
the optional update module (updater/trimmer_update.py, see core/updates.py); this file only shows
windows and runs that module's functions in the background."""

import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QMessageBox, QProgressDialog, QPushButton,
                               QTextBrowser, QVBoxLayout)

from .. import __version__
from ..core import updates as U
from .theme import set_kind
from .widgets import label


SETTINGS_HINT = "You can check for updates any time in Settings > Updates."
WHAT_IS_SENT = ("Trimmer only asks GitHub for the latest release, like opening the Releases page in a browser. "
                "Nothing about you, your clips or your settings is sent.")
LAUNCH_DELAY_MS = 4000


class Background(QObject):
    """Runs fn on a daemon thread and reports back on the GUI thread. A daemon thread, unlike a
    QThread, never holds up or crashes quitting while GitHub is slow to answer."""
    done = Signal(object)
    failed = Signal(str)
    step = Signal(int, int)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:                 # noqa: BLE001 (shown to the user, or ignored on launch)
            self.failed.emit(str(e) or type(e).__name__)


def open_releases_page():
    QDesktopServices.openUrl(QUrl(U.RELEASES_PAGE))       # the user's own browser, not Trimmer


def ask_permission(parent) -> bool:
    """The first-launch question (the zip and source copies; the installer asks its own)."""
    box = QMessageBox(QMessageBox.Question, "Check for updates?",
                      "Should Trimmer check GitHub for new versions when it opens?", parent=parent)
    box.setInformativeText(WHAT_IS_SENT + " Choose No and Trimmer never connects to the internet.\n\n"
                           + SETTINGS_HINT)
    yes = box.addButton("Yes, check for updates", QMessageBox.YesRole)
    box.addButton("No", QMessageBox.NoRole)
    box.setDefaultButton(yes)
    box.exec()
    return box.clickedButton() is yes


class UpdateDialog(QDialog):
    """"Trimmer x is available": Install update / Skip this version / Don't ask again."""

    def __init__(self, release: dict, can_install: bool, parent=None):
        super().__init__(parent)
        self.choice = "later"
        new = U.version_text(release["tag"])
        self.setWindowTitle("Update available")
        self.resize(560, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        lay.addWidget(label(f"Trimmer {new} is available (you have {__version__}).", "title"))
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(release.get("notes") or "No release notes.")
        lay.addWidget(notes, 1)
        lay.addWidget(label(SETTINGS_HINT, "muted", wrap=True))
        row = QHBoxLayout()
        self.b_never = QPushButton("Don't ask again")
        self.b_never.setToolTip("Turn off update checks. Turn them back on in Settings > Updates.")
        self.b_skip = QPushButton("Skip this version")
        self.b_skip.setToolTip(f"No popup for {new}. You'll hear about the next version after it.")
        self.b_install = set_kind(QPushButton("Install update" if can_install else "Open download page"), "primary")
        self.b_install.setToolTip("Download the installer, check it, install it and reopen Trimmer"
                                  if can_install else "Open the release page in your browser")
        row.addWidget(self.b_never)
        row.addStretch(1)
        row.addWidget(self.b_skip)
        row.addWidget(self.b_install)
        lay.addLayout(row)
        for b, key in ((self.b_never, "never"), (self.b_skip, "skip"), (self.b_install, "install")):
            b.clicked.connect(lambda _=False, key=key: self._pick(key))
        self.b_install.setDefault(True)

    def _pick(self, key):
        self.choice = key
        self.accept()


class Updater(QObject):
    """Owned by the main window. Nothing here runs unless the user has said yes to update checks
    (or pressed Check now)."""
    checked = Signal(str)          # a line for Settings: when it last checked, or what went wrong
    turned_off = Signal()          # "Don't ask again" was pressed

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self.tasks = []
        self.checking = False
        self.pending = None        # a release to install once the queue finishes
        self._cancel = False

    @property
    def settings(self):
        return self.win.settings

    def available(self) -> bool:
        return U.available()

    def can_install(self) -> bool:
        return U.install_kind() == "installed" and sys.platform == "win32"

    # ---------------------------------------------------------------- launch
    def on_launch(self):
        U.clean_downloads()                    # local files only: an installer an earlier update left
        what = U.should_check_on_launch(self.settings)
        if what == "check" and self.settings.update_checks == "ask":
            self.settings.update_checks = "on"          # said yes in the installer
            self.settings.save()
        if what == "ask":
            what = "check" if self.ask_first() else "no"
        if what == "check":
            QTimer.singleShot(LAUNCH_DELAY_MS, lambda: self.check(manual=False))

    def ask_first(self) -> bool:
        on = ask_permission(self.win)
        self.settings.update_checks = "on" if on else "off"
        self.settings.save()
        return on

    # ----------------------------------------------------------------- check
    def check(self, manual=True):
        if self.checking:
            return
        mod = U.load_module()
        if mod is None:
            if manual:
                QMessageBox.information(self.win, "Updates", "This copy of Trimmer was installed with local "
                                        "features only, so it can't check for updates.")
            return
        self.checking = True
        t = Background(mod.latest_release, parent=self)
        t.done.connect(lambda rel: self._checked(rel, manual))
        t.failed.connect(lambda err: self._failed(err, manual))
        self.tasks.append(t)
        t.start()

    def _failed(self, err, manual):
        self.checking = False
        self.checked.emit(f"Couldn't reach GitHub: {err}")
        if manual:
            QMessageBox.warning(self.win, "Updates", f"Couldn't check for updates:\n{err}")

    def _checked(self, release, manual):
        self.checking = False
        self.settings.last_update_check = time.strftime("%d %b %Y, %H:%M")
        self.settings.save()
        self.checked.emit(f"Last checked {self.settings.last_update_check}")
        tag = release.get("tag", "")
        if not U.is_newer(tag, __version__):
            if manual:
                QMessageBox.information(self.win, "Updates", f"You have the latest version ({__version__}).")
            return
        if not manual and U.version_text(tag) == self.settings.skipped_version:
            return
        self.offer(release)

    def offer(self, release):
        can = self.can_install() and U.setup_asset(release) is not None and "SHA256SUMS.txt" in release["assets"]
        d = UpdateDialog(release, can, self.win)
        d.exec()
        if d.choice == "never":
            self.settings.update_checks = "off"
            self.settings.save()
            self.turned_off.emit()
        elif d.choice == "skip":
            self.settings.skipped_version = U.version_text(release["tag"])
            self.settings.save()
        elif d.choice == "install":
            if can:
                self.install(release)
            else:
                QDesktopServices.openUrl(QUrl(release.get("page") or U.RELEASES_PAGE))

    # --------------------------------------------------------------- install
    def install(self, release):
        if self.win.queue.is_running():
            box = QMessageBox(QMessageBox.Question, "The queue is still running",
                              "Installing closes Trimmer. Install the update when the queue finishes?",
                              parent=self.win)
            later = box.addButton("Install when the queue finishes", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is later:
                self.pending = release
                self.win.queue.running_changed.connect(self._queue_changed)
                self.win.show_status("The update will install when the queue finishes.")
            return
        self._download(release)

    def _queue_changed(self, running):
        if running or self.pending is None:
            return
        release, self.pending = self.pending, None
        self.win.queue.running_changed.disconnect(self._queue_changed)
        QTimer.singleShot(0, lambda: self._download(release))

    def _download(self, release):
        mod = U.load_module()
        name = U.setup_asset(release)
        folder = U.download_folder()
        folder.mkdir(exist_ok=True)
        dest = folder / name
        self._cancel = False
        busy = QProgressDialog(f"Downloading Trimmer {U.version_text(release['tag'])}…", "Cancel", 0, 100, self.win)
        busy.setWindowTitle("Installing update")
        busy.setWindowModality(Qt.WindowModal)
        busy.setMinimumDuration(0)
        busy.canceled.connect(lambda: setattr(self, "_cancel", True))
        busy.setValue(0)                       # on screen straight away

        def work():
            sums = folder / "SHA256SUMS.txt"
            mod.download(release["assets"]["SHA256SUMS.txt"], sums, cancelled=lambda: self._cancel)
            mod.download(release["assets"][name], dest, cancelled=lambda: self._cancel,
                         progress=lambda d, n: t.step.emit(d // 1024, n // 1024))
            text = sums.read_text(encoding="utf-8", errors="replace")
            sums.unlink(missing_ok=True)
            if not U.checksum_ok(dest, name, text):
                dest.unlink(missing_ok=True)
                raise ValueError("the downloaded installer doesn't match the release's checksum, so it "
                                 "wasn't run. Try again, or download it from the Releases page.")
            return dest

        def step(done_kb, total_kb):
            busy.setMaximum(total_kb)          # 0 = unknown size: a moving bar
            if total_kb:
                busy.setValue(min(done_kb, total_kb))

        t = Background(work, parent=self)
        t.step.connect(step)
        t.done.connect(lambda path: (busy.reset(), self._run_installer(path)))
        t.failed.connect(lambda err: (busy.reset(), None if self._cancel else
                                      QMessageBox.warning(self.win, "Update", f"The update wasn't installed: {err}")))
        self.tasks.append(t)
        t.start()

    def _run_installer(self, path):
        # Close first (it may ask to save what's open); only then start the installer.
        if not self.win.close():
            return
        flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0   # DETACHED_PROCESS | NEW_GROUP
        try:
            subprocess.Popen([str(path), *U.installer_args()], creationflags=flags, close_fds=True)
        except OSError as e:
            QMessageBox.warning(None, "Update", f"Couldn't start the installer: {e}\n\nIt was saved to:\n{path}")
        QApplication.instance().quit()
