"""Qt's video player, or the silent frame-by-frame fallback when it can't load.

Windows can stop Qt's multimedia DLLs from loading (Smart App Control, antivirus, a
broken install). Trimmer then still works, with a slower preview and no sound, and the
reason is kept here so the app can say so and write it to the log.
"""

import os
import sys
import traceback

PLAYBACK_ERROR = ""
try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAVE_PLAYBACK = True
except (ImportError, OSError) as e:
    from .fallback_player import FramePlayer as QMediaPlayer
    from .fallback_player import FrameView as QVideoWidget  # noqa: F401  (re-exported)
    QAudioOutput = None
    HAVE_PLAYBACK = False
    PLAYBACK_ERROR = "".join(traceback.format_exception_only(type(e), e)).strip()

NO_SOUND = "No sound (hover for why)"
NO_SOUND_TIP = ("Windows didn't let Qt's video player load, so the preview shows frames one at a time "
                "and has no sound. Saving, trimming and merging are not affected.\n\n"
                "The reason is in the Logs folder (playback.txt):\n")


def report() -> str:
    """A plain text report on why playback can or can't load, for playback.txt and the build check."""
    lines = [f"Playback: {'OK' if HAVE_PLAYBACK else 'FAILED'}"]
    if PLAYBACK_ERROR:
        lines.append(f"Error: {PLAYBACK_ERROR}")
    lines.append(f"Python {sys.version.split()[0]}, frozen: {bool(getattr(sys, 'frozen', False))}")
    try:
        import PySide6
        folder = os.path.dirname(PySide6.__file__)
        lines.append(f"PySide6 {PySide6.__version__} in {folder}")
    except Exception as e:      # noqa: BLE001
        lines.append(f"PySide6 not importable: {e}")
        return "\n".join(lines)
    if sys.platform == "win32":
        # Load the multimedia DLLs one by one: the first that fails is the culprit.
        import ctypes
        names = sorted(n for n in os.listdir(folder)
                       if n.lower().endswith((".dll", ".pyd"))
                       and any(k in n.lower() for k in ("multimedia", "avcodec", "avformat", "avutil",
                                                        "swresample", "swscale", "network", "concurrent")))
        plugins = os.path.join(folder, "plugins", "multimedia")
        if os.path.isdir(plugins):
            names += [os.path.join("plugins", "multimedia", n) for n in sorted(os.listdir(plugins))]
        else:
            lines.append("plugins\\multimedia folder is missing")
        os.add_dll_directory(folder)
        for n in names:
            try:
                ctypes.WinDLL(os.path.join(folder, n))
                lines.append(f"  loads   {n}")
            except OSError as e:
                lines.append(f"  FAILS   {n}: {e}")
    return "\n".join(lines)


def write_report(path=None):
    """Write playback.txt to the Logs folder when playback failed (and remove an old one when it works)."""
    from ..core.util import logs_dir
    p = path or (logs_dir() / "playback.txt")
    try:
        if HAVE_PLAYBACK and path is None:
            if p.exists():
                p.unlink()
            return None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(report() + "\n", encoding="utf-8")
        return p
    except OSError:
        return None


def check(out_path) -> int:
    """Trimmer.exe --check-playback FILE: the build's self test. Writes the report, 0 when playback loads."""
    from pathlib import Path
    text = report()
    if HAVE_PLAYBACK:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])  # noqa: F841
        player = QMediaPlayer()
        text += f"\nMedia backend available: {player.isAvailable()}"
    Path(out_path).write_text(text + "\n", encoding="utf-8")
    return 0 if HAVE_PLAYBACK else 1
