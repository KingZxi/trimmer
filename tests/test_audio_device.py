"""Preview sound follows the system's default output device (headphones -> speakers)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from trimmer.gui import audio_device  # noqa: E402

app = QApplication.instance() or QApplication([])


class Device:
    def __init__(self, name):
        self.name = name

    def id(self):
        return self.name.encode()

    def isNull(self):
        return not self.name


class Output(QObject):
    gone = Signal()

    def __init__(self, device):
        super().__init__()
        self.dev, self.moves = device, 0

    def device(self):
        return self.dev

    def setDevice(self, device):
        self.dev, self.moves = device, self.moves + 1


def test_outputs_move_to_the_new_default(monkeypatch):
    current = {"dev": Device("headphones")}
    monkeypatch.setattr(audio_device, "_default_device", lambda: current["dev"])
    follower = audio_device.OutputFollower()
    a, b = Output(Device("headphones")), Output(Device("headphones"))
    follower.add(a)
    follower.add(b)
    assert a.moves == b.moves == 0 and follower.timer.isActive()

    current["dev"] = Device("speakers")         # user switches the output in Windows
    follower.sync()
    assert a.device().name == b.device().name == "speakers" and a.moves == b.moves == 1
    follower.sync()                             # nothing changed: no needless re-opening
    assert a.moves == 1

    current["dev"] = Device("")                 # every device unplugged: leave them be
    follower.sync()
    assert a.device().name == "speakers"

    a.deleteLater()                             # a closed viewer drops out
    app.processEvents()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert follower.outputs == [b]
    b.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert follower.outputs == [] and not follower.timer.isActive()


def test_real_audio_outputs_register():
    from PySide6.QtMultimedia import QAudioOutput
    out = QAudioOutput()
    audio_device.follow_default_output(out)
    audio_device.follow_default_output(None)
    assert out in audio_device._follower.outputs
    audio_device._follower.sync()               # no devices here: must not raise
