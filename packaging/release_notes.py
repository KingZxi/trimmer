"""Prints the GitHub Release text for the current version: its CHANGELOG.md
section plus download notes. Used by .github/workflows/release.yml."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trimmer import __version__  # noqa: E402

log = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
match = re.search(rf"^## {re.escape(__version__)}\b.*?$\n(.*?)(?=^## |\Z)", log, re.M | re.S)
if not match:
    sys.exit(f"CHANGELOG.md has no section for {__version__}")

print(f"""{match.group(1).strip()}

## Download

- **Trimmer-{__version__}-setup.exe**: installer. No admin rights needed.
- **Trimmer-{__version__}-portable.zip**: unzip anywhere and run `Trimmer.exe`.

Trimmer needs ffmpeg. On first launch it shows how to get it (`winget install Gyan.FFmpeg`).

The download isn't code-signed yet, so Windows SmartScreen may say "Windows protected
your PC". Click **More info**, then **Run anyway**. With Smart App Control turned on,
Windows blocks unsigned apps completely; run Trimmer from source instead (see the README).
`SHA256SUMS.txt` lists the checksums of the files.""")
