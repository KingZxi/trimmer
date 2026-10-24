"""Trimmer: trim, merge, compress and rip OBS clips, fully offline.

Run:  python trimmer.py  [files or folders to open]
Or double-click Trimmer.pyw to start it without a console window.
"""

import sys
from pathlib import Path

ICON = Path(__file__).resolve().parent / "trimmer" / "gui" / "assets" / "trimmer.ico"


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
    app.setApplicationName("Trimmer")
    if ICON.exists():
        app.setWindowIcon(QIcon(str(ICON)))
    settings = AppSettings.load()
    apply_theme(app, settings.theme)

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
    win.startup_checks()
    if len(sys.argv) > 1:
        win._route(sys.argv[1:])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
