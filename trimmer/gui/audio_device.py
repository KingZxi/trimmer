"""Keeps preview sound on the output device Windows is using now.

A QAudioOutput made without a device picks the system default once, when it's
created, and stays on that device. So after switching from headphones to
speakers in Windows, Trimmer kept playing through the headphones until it was
restarted. follow_default_output() moves every preview's sound to the new
default as soon as it changes (Qt's device-change signal, plus a light check
every couple of seconds in case a driver doesn't report the switch)."""

from PySide6.QtCore import QObject, QTimer

try:        # Smart App Control can block Qt's multimedia DLLs (see clips_tab)
    from PySide6.QtMultimedia import QMediaDevices
except (ImportError, OSError):
    QMediaDevices = None

POLL_MS = 2000


def _default_device():
    return QMediaDevices.defaultAudioOutput() if QMediaDevices is not None else None


def _device_id(device):
    try:
        return None if device is None or device.isNull() else bytes(device.id())
    except RuntimeError:        # deleted underneath us
        return None


class OutputFollower(QObject):
    """One per app: every registered QAudioOutput is moved to the current default device."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.outputs = []
        self.devices = QMediaDevices(self) if QMediaDevices is not None else None
        if self.devices is not None:
            self.devices.audioOutputsChanged.connect(self.sync)
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.sync)

    def add(self, output):
        if output is None or output in self.outputs:
            return
        self.outputs.append(output)
        try:
            output.destroyed.connect(lambda *_: self._drop(output))
        except (AttributeError, RuntimeError):
            pass
        self.sync()
        if not self.timer.isActive():
            self.timer.start()

    def _drop(self, output):
        if output in self.outputs:
            self.outputs.remove(output)
        if not self.outputs:
            try:
                self.timer.stop()
            except RuntimeError:    # app shutting down: the timer is already gone
                pass

    def sync(self):
        """Point every output at the default device if it isn't there already."""
        device = _default_device()
        want = _device_id(device)
        if want is None:            # no output device at all right now: leave things as they are
            return
        for output in list(self.outputs):
            try:
                if _device_id(output.device()) != want:
                    output.setDevice(device)
            except RuntimeError:    # the C++ object went away before destroyed() reached us
                self._drop(output)


_follower = None


def follow_default_output(output):
    """Keep `output` (a QAudioOutput, or None) on the system's default output device."""
    global _follower
    if output is None:
        return
    if _follower is None:
        _follower = OutputFollower()
    _follower.add(output)
