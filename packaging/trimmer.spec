# PyInstaller recipe for the Windows app. Build with packaging\build.ps1
# (or: pyinstaller --noconfirm packaging\trimmer.spec from the repo folder).
# Makes dist\Trimmer\Trimmer.exe with everything it needs beside it.

from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "trimmer.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "trimmer" / "gui" / "assets"), "trimmer/gui/assets"),
        (str(ROOT / "LICENSE"), "."),
    ],
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
