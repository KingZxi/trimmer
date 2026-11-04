# PyInstaller recipe for the Windows app. Build with packaging\build.ps1
# (or: pyinstaller --noconfirm packaging\trimmer.spec from the repo folder).
# Makes dist\Trimmer\Trimmer.exe with everything it needs beside it.

from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

ROOT = Path(SPECPATH).parent

# Each package's dist-info folder carries its licence texts (see THIRD-PARTY-NOTICES.md).
# build.ps1 puts LICENSE, LICENSES\ and the notices next to Trimmer.exe.
licences = []
for dist in ("numpy", "send2trash", "PySide6", "PySide6_Essentials", "shiboken6"):
    try:
        licences += copy_metadata(dist)
    except Exception:
        pass

a = Analysis(
    [str(ROOT / "trimmer.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "trimmer" / "gui" / "assets"), "trimmer/gui/assets"),
    ] + licences,
    # Qt parts Trimmer never uses, left out to keep the download small.
    excludes=["tkinter", "pytest", "tests", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtNetwork"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Trimmer",
    console=False,
    icon=str(ROOT / "trimmer" / "gui" / "assets" / "trimmer.ico"),
    version=str(ROOT / "packaging" / "version_info.txt") if (ROOT / "packaging" / "version_info.txt").exists() else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Trimmer", upx=False)
