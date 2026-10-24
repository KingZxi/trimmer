"""Watch a folder for new OBS replays and announce each once it is finished.

OBS writes a replay over a second or two, so a new file is only announced
after its size and modified time stay put for `stable_secs`, it opens for
reading (an exclusively locked file raises PermissionError on Windows) and
ffprobe can read it. Nothing here sleeps; a timer polls the pending files.
"""

import re
import time
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from ..core import ffmpeg as ff
from ..core import merge as mg
from ..core.util import VIDEO_EXTENSIONS
from .worker import Task

# A timestamp, then at most OBS's own " (2)" duplicate marker. Trimmer's
# outputs ("(trimmed)", "(merged)", quality tags, ".part") carry more.
_REPLAY_RE = re.compile(r"^[^()]*?" + mg._TIMESTAMP_RE.pattern + r"(?: \(\d+\))?$")
MAX_TRIES = 5


def is_replay_name(name: str) -> bool:
    """True for names that look like an OBS replay video (not a .part file)."""
    p = Path(name)
    if p.suffix.lower() not in VIDEO_EXTENSIONS or p.stem.lower().endswith(".part"):
        return False
    return bool(_REPLAY_RE.match(p.stem)) and mg.parse_name_timestamp(p.stem) is not None


def _default_validate(path: Path):
    ff.probe(path)


class FolderWatcher(QObject):
    ready = Signal(list)        # list[Path]: finished new replays
    lost = Signal(str)          # the folder went away; watching has stopped

    def __init__(self, stable_secs=2.0, poll_ms=500, rescan_secs=5.0, validate=_default_validate, parent=None):
        super().__init__(parent)
        self.stable_secs = stable_secs
        self.validate = validate
        self.folder = None
        self.known = set()          # seen at start, announced, or given up on
        self.pending = {}           # path -> [size, mtime, stable_since, tries]
        self._checking = {}         # path -> Task still running
        self.fsw = QFileSystemWatcher(self)
        self.fsw.directoryChanged.connect(lambda _p: self.rescan())
        self.poll = QTimer(self)
        self.poll.setInterval(poll_ms)
        self.poll.timeout.connect(self._poll)
        # Network drives and some filters don't raise directoryChanged.
        self.slow = QTimer(self)
        self.slow.setInterval(int(rescan_secs * 1000))
        self.slow.timeout.connect(self.rescan)

    @property
    def active(self) -> bool:
        return self.folder is not None

    def start(self, folder) -> bool:
        """Watch `folder`; files already in it are ignored. False if it can't be read."""
        self.stop()
        folder = Path(folder)
        try:
            self.known = {p for p in folder.iterdir()}
        except OSError:
            return False
        self.folder = folder
        self.fsw.addPath(str(folder))
        self.slow.start()
        return True

    def stop(self):
        if self.fsw.directories():
            self.fsw.removePaths(self.fsw.directories())
        self.poll.stop()
        self.slow.stop()
        self.folder = None
        self.pending.clear()

    def rescan(self):
        """Diff the folder listing against what is already known."""
        if self.folder is None:
            return
        try:
            names = list(self.folder.iterdir())
        except OSError:
            gone = str(self.folder)
            self.stop()
            self.lost.emit(gone)
            return
        if str(self.folder) not in self.fsw.directories():
            self.fsw.addPath(str(self.folder))      # re-arm if it was dropped
        present = set(names)
        for p in names:
            if p not in self.known and p not in self.pending and is_replay_name(p.name):
                self.pending[p] = [None, None, time.monotonic(), 0]
        # a pending file that was removed or renamed away
        for p in [p for p in self.pending if p not in present]:
            del self.pending[p]
        if self.pending:
            self.poll.start()

    def _poll(self):
        now = time.monotonic()
        for p in list(self.pending):
            st = self.pending[p]
            if p in self._checking:
                continue
            try:
                s = p.stat()
            except OSError:
                del self.pending[p]         # removed before it was finished
                continue
            if (s.st_size, s.st_mtime) != (st[0], st[1]):
                st[0], st[1], st[2] = s.st_size, s.st_mtime, now
                continue
            if now - st[2] < self.stable_secs:
                continue
            try:
                with open(p, "rb") as f:
                    f.read(1)
            except OSError:
                st[2] = now                 # still locked by OBS
                continue
            t = Task(self.validate, p, parent=self)
            t.done.connect(lambda _r, p=p: self._validated(p, True))
            t.failed.connect(lambda _m, p=p: self._validated(p, False))
            t.finished.connect(lambda p=p: self._checking.pop(p, None))
            self._checking[p] = t
            t.start()
        if not self.pending:
            self.poll.stop()

    def _validated(self, p, ok):
        st = self.pending.get(p)
        if st is None:
            return
        if ok:
            del self.pending[p]
            self.known.add(p)
            self.ready.emit([p])
            return
        st[3] += 1
        st[2] = time.monotonic()
        if st[3] >= MAX_TRIES:              # never becomes readable: stop retrying
            del self.pending[p]
            self.known.add(p)
