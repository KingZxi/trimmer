"""Update check plumbing that never touches the network: finding the optional update module,
comparing versions, picking the installer from a release, checking its checksum, and deciding
whether to check at all. The network part is updater/trimmer_update.py, which the installer can
leave out ("Local features only")."""

import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import Optional

UPDATE_MODULE = "trimmer_update.py"
RELEASES_PAGE = "https://github.com/KingZxi/trimmer/releases/latest"   # opened in the user's browser


def app_root() -> Path:
    """Where Trimmer's own files are: the packaged app's _internal folder, or the source folder."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))


def module_path(root: Path = None) -> Path:
    return (root or app_root()) / "updater" / UPDATE_MODULE


def available(root: Path = None) -> bool:
    """False when Trimmer was installed with local features only (no update code at all)."""
    return module_path(root).is_file()


def load_module(root: Path = None):
    """The update module, or None when it isn't installed."""
    p = module_path(root)
    if not p.is_file():
        return None
    spec = importlib.util.spec_from_file_location("trimmer_update", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_module(report: str) -> int:
    """Build self test (release.yml): load the update file and what it needs, without connecting.
    Writes a line to `report`; 0 when it's all there."""
    try:
        mod = load_module()
        if mod is None:
            raise FileNotFoundError(f"no {module_path()}")
        import ssl
        ssl.create_default_context()           # HTTPS works in this build
        ok, text = 0, f"Updater: OK ({mod.API_LATEST})"
    except Exception as e:                     # noqa: BLE001 (any failure is the answer)
        ok, text = 1, f"Updater: FAILED ({type(e).__name__}: {e})"
    Path(report).write_text(text + "\n", encoding="utf-8")
    return ok


def download_folder() -> Path:
    import tempfile
    return Path(tempfile.gettempdir()) / "Trimmer-update"


def clean_downloads():
    """Remove installers left from an earlier Install update (they've done their job)."""
    for f in download_folder().glob("*"):
        try:
            f.unlink()
        except OSError:
            pass


def install_kind(exe: Path = None, frozen: bool = None) -> str:
    """"installed" (the installer put it there, so updates can install themselves), "portable"
    (the zip) or "source" (run with Python)."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen:
        return "source"
    folder = Path(exe or sys.executable).parent
    return "installed" if any(folder.glob("unins*.exe")) else "portable"


def parse_version(text: str) -> Optional[tuple]:
    """"v1.10.2" -> (1, 10, 2). None for anything that isn't a plain release number."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", (text or "").strip())
    if not m:
        return None
    return tuple(int(x or 0) for x in m.groups())


def is_newer(tag: str, current: str) -> bool:
    new, cur = parse_version(tag), parse_version(current)
    return new is not None and cur is not None and new > cur


def version_text(tag: str) -> str:
    v = parse_version(tag)
    return ".".join(map(str, v)) if v else tag


def setup_asset(release: dict) -> Optional[str]:
    """The release's installer file name (Trimmer-x.y.z-setup.exe), or None."""
    want = f"Trimmer-{version_text(release.get('tag', ''))}-setup.exe".lower()
    for name in release.get("assets", {}):
        if name.lower() == want:
            return name
    return None


def parse_sums(text: str) -> dict:
    """SHA256SUMS.txt ("<hash>  <file name>" per line) -> {file name: hash}."""
    out = {}
    for line in text.splitlines():
        m = re.fullmatch(r"\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def checksum_ok(path: Path, name: str, sums_text: str) -> bool:
    want = parse_sums(sums_text).get(name)
    return bool(want) and sha256_of(path) == want


def should_check_on_launch(settings, kind: str = None, has_module: bool = None) -> str:
    """"check", "ask" (first launch of the zip or a source copy) or "no"."""
    has_module = available() if has_module is None else has_module
    if not has_module:
        return "no"
    choice = settings.update_checks
    if choice == "ask":
        # The installer already asked; only the zip and source copies ask on first launch.
        return "check" if (kind or install_kind()) == "installed" else "ask"
    return "check" if choice == "on" else "no"


def installer_args() -> list:
    """Run the downloaded installer without its wizard, close Trimmer for it, and reopen Trimmer."""
    # /NORESTARTAPPLICATIONS: if Windows still had Trimmer open, the installer mustn't reopen it as well
    # as /RELAUNCH doing so (two windows).
    return ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/NORESTARTAPPLICATIONS",
            "/RELAUNCH=1"]
