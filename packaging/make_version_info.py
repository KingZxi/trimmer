"""Writes packaging/version_info.txt (the Details tab of Trimmer.exe's
Properties: version, author, copyright) from trimmer/__init__.py."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trimmer import APP_NAME, AUTHOR, COPYRIGHT, __version__  # noqa: E402

nums = tuple(int(x) for x in (__version__.split(".") + ["0", "0", "0"])[:4])
text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{AUTHOR}'),
      StringStruct('FileDescription', '{APP_NAME}: trim, merge, compress and rip OBS clips'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', '{APP_NAME}'),
      StringStruct('LegalCopyright', '{COPYRIGHT} (GPL-3.0)'),
      StringStruct('OriginalFilename', '{APP_NAME}.exe'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
(ROOT / "packaging" / "version_info.txt").write_text(text, encoding="utf-8")
print(__version__)
