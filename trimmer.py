"""Trimmer: trim, merge, compress and rip OBS clips, fully offline.

Run:  python trimmer.py  [files or folders to open]
Or double-click Trimmer.pyw to start it without a console window.
"""

import sys

sys.dont_write_bytecode = True      # no __pycache__ folders in the Trimmer folder

from pathlib import Path

# Packaged builds (PyInstaller) unpack their files to sys._MEIPASS.
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ICON = ROOT / "trimmer" / "gui" / "assets" / "trimmer.ico"


def _splash():
    """A small splash with the logo and name, shown while the main window loads."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication, QSplashScreen
    png = ICON.with_suffix(".png")
    if not png.exists():
        return None
    dpr = QApplication.primaryScreen().devicePixelRatio() if QApplication.primaryScreen() else 1.0
    w, h = 320, 200
    pm = QPixmap(int(w * dpr), int(h * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(QColor("#17181c"))
    p = QPainter(pm)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.drawPixmap((w - 88) // 2, 34, 88, 88, QPixmap(str(png)))
    font = QFont()
    font.setPointSize(15)
    font.setBold(True)
    p.setFont(font)
    p.setPen(QColor("#f2f3f5"))
    p.drawText(0, 132, w, 28, Qt.AlignHCenter | Qt.AlignVCenter, "Trimmer")
    font.setPointSize(9)
    font.setBold(False)
    p.setFont(font)
    p.setPen(QColor("#8b8f98"))
    p.drawText(0, 160, w, 20, Qt.AlignHCenter | Qt.AlignVCenter, "Loading…")
    p.end()
    splash = QSplashScreen(pm)
    splash.show()
    QApplication.processEvents()
    return splash


def main():
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    from trimmer.core.journal import write_crash_log
    from trimmer.core.settings import AppSettings
    from trimmer.gui.main_window import MainWindow
    from trimmer.gui.widgets import apply_theme

    if sys.platform == "win32":
        # Its own taskbar entry and icon, instead of grouping under python(w).exe.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Trimmer.App")
        except (AttributeError, OSError):
            pass
    app = QApplication(sys.argv)
    from trimmer.core import ffmpeg as ff
    app.aboutToQuit.connect(ff.shutdown)    # no ffmpeg may keep running once Trimmer quits
    from trimmer import __version__
    app.setApplicationName("Trimmer")
    app.setApplicationVersion(__version__)
    if ICON.exists():
        icon = QIcon(str(ICON))
        png = ICON.with_suffix(".png")
        if png.exists():
            icon.addFile(str(png))           # crisp at large sizes (Alt+Tab, high DPI taskbars)
        app.setWindowIcon(icon)
    settings = AppSettings.load()
    apply_theme(app, settings.theme)
    splash = _splash()                       # the logo while the window is being built

    def excepthook(exc_type, exc, tb):
        path = write_crash_log(exc_type, exc, tb)
        if sys.stderr is not None:          # None when started windowless (pythonw)
            sys.__excepthook__(exc_type, exc, tb)
        try:
            QMessageBox.critical(None, "Trimmer hit an error",
                                 f"{exc_type.__name__}: {exc}\n\nDetails were saved to:\n{path}\n\n"
                                 "Your original files are untouched. Unfinished outputs are only ever "
                                 "written as .part files.")
        except Exception:
            pass

    sys.excepthook = excepthook
    win = MainWindow(settings)
    win.show()
    if splash is not None:
        splash.finish(win)
    win.startup_checks()
    if len(sys.argv) > 1:
        win._route(sys.argv[1:])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
