"""Double-click to start Trimmer without a console window.

Windows opens .pyw files with pythonw.exe, the windowless Python that is
installed (and signed) alongside python.exe, so Smart App Control allows it.
Right-click > Send to > Desktop (create shortcut) for a desktop icon, or pin
the running app to the taskbar.
"""

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent / "trimmer.py"), run_name="__main__")
