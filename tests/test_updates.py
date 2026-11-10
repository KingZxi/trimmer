"""The optional update check: versions, the installer and its checksum, when it may check, the
update file's GitHub reply, and the windows (first-launch question, popup, Settings > Updates)."""

import hashlib
import importlib.util
import io
import json
import shutil
from pathlib import Path

# First: this sets up the offscreen app and the tests' own data folder.
from tests.gui_helpers import WORK, app, pump  # noqa: F401
import pytest
from PySide6.QtWidgets import QLabel

from trimmer.core import updates as U
from trimmer.core.settings import AppSettings
from trimmer.gui import updates as GU
from trimmer.gui.main_window import MainWindow
from trimmer.gui.settings_dialog import SettingsDialog

ROOT = Path(__file__).resolve().parents[1]


def load_update_file():
    spec = importlib.util.spec_from_file_location("trimmer_update_test", ROOT / "updater" / "trimmer_update.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_versions():
    assert U.parse_version("v1.3.0") == (1, 3, 0) and U.parse_version("1.10") == (1, 10, 0)
    assert U.parse_version("v1.3.0-beta") is None and U.parse_version("") is None
    assert U.is_newer("v1.10.0", "1.9.9") and not U.is_newer("v1.2.0", "1.2.0")
    assert not U.is_newer("v1.1.9", "1.2.0") and not U.is_newer("nightly", "1.2.0")
    assert U.version_text("v1.3.0") == "1.3.0"


def test_installer_and_checksum(tmp_path):
    rel = {"tag": "v1.3.0", "assets": {"Trimmer-1.3.0-portable.zip": "z", "Trimmer-1.3.0-setup.exe": "s",
                                       "SHA256SUMS.txt": "h"}}
    assert U.setup_asset(rel) == "Trimmer-1.3.0-setup.exe"
    assert U.setup_asset({"tag": "v1.4.0", "assets": rel["assets"]}) is None
    f = tmp_path / "Trimmer-1.3.0-setup.exe"
    f.write_bytes(b"installer bytes")
    good = hashlib.sha256(b"installer bytes").hexdigest()
    sums = f"{'0' * 64}  Trimmer-1.3.0-portable.zip\n{good}  Trimmer-1.3.0-setup.exe\n"
    assert U.parse_sums(sums)["Trimmer-1.3.0-setup.exe"] == good
    assert U.checksum_ok(f, f.name, sums)
    assert not U.checksum_ok(f, f.name, sums.replace(good, "f" * 64))    # tampered or broken
    assert not U.checksum_ok(f, f.name, "")                              # no checksum, never run


def test_when_it_checks(tmp_path):
    s = AppSettings()
    assert s.update_checks == "ask"
    assert U.should_check_on_launch(s, "portable", True) == "ask"       # the zip asks on first launch
    assert U.should_check_on_launch(s, "source", True) == "ask"
    assert U.should_check_on_launch(s, "installed", True) == "check"    # the installer already asked
    assert U.should_check_on_launch(s, "installed", False) == "no"      # local features only
    s.update_checks = "off"
    assert U.should_check_on_launch(s, "installed", True) == "no"
    s.update_checks = "on"
    assert U.should_check_on_launch(s, "portable", True) == "check"
    assert U.should_check_on_launch(s, "portable", False) == "no"
    # Local features only = the update file isn't there at all.
    assert not U.available(tmp_path) and U.load_module(tmp_path) is None
    (tmp_path / "updater").mkdir()
    shutil.copy(ROOT / "updater" / "trimmer_update.py", tmp_path / "updater")
    assert U.available(tmp_path) and U.load_module(tmp_path).REPO == "KingZxi/trimmer"
    assert U.available()                       # this source copy has it


def test_install_kind(tmp_path):
    exe = tmp_path / "Trimmer.exe"
    assert U.install_kind(exe, frozen=False) == "source"
    assert U.install_kind(exe, frozen=True) == "portable"
    (tmp_path / "unins000.exe").write_bytes(b"")
    assert U.install_kind(exe, frozen=True) == "installed"


def test_settings_keep_a_valid_choice():
    s = AppSettings()
    s.update_checks = "maybe"
    s.save()
    assert AppSettings.load().update_checks == "ask"
    AppSettings().save()


class Reply(io.BytesIO):
    def __init__(self, data, length=None):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data) if length is None else length)}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_update_file_reads_the_release(monkeypatch, tmp_path):
    mod = load_update_file()
    asked = []
    body = {"tag_name": "v1.3.0", "name": "Trimmer v1.3.0", "body": "## New\n- Things", "draft": False,
            "html_url": "https://github.com/KingZxi/trimmer/releases/tag/v1.3.0",
            "assets": [{"name": "Trimmer-1.3.0-setup.exe",
                        "browser_download_url": mod.DOWNLOAD_PREFIX + "v1.3.0/Trimmer-1.3.0-setup.exe"},
                       {"name": "evil.exe", "browser_download_url": "https://example.com/evil.exe"}]}

    def fake(req, timeout):
        asked.append((req.full_url, req.get_method(), req.data, dict(req.header_items())))
        if req.full_url == mod.API_LATEST:
            return Reply(json.dumps(body).encode())
        return Reply(b"x" * 1000)

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake)
    rel = mod.latest_release()
    assert rel["tag"] == "v1.3.0" and rel["notes"].startswith("## New")
    assert list(rel["assets"]) == ["Trimmer-1.3.0-setup.exe"]          # only files from Trimmer's releases
    url, method, data, headers = asked[0]
    assert method == "GET" and data is None                            # nothing is sent
    assert headers["User-agent"] == "Trimmer-update-check"            # no version, no user details
    assert headers["Accept"] == "application/vnd.github+json"
    seen = []
    out = mod.download(rel["assets"]["Trimmer-1.3.0-setup.exe"], tmp_path / "s.exe",
                       progress=lambda d, n: seen.append((d, n)))
    assert out.read_bytes() == b"x" * 1000 and seen[-1] == (1000, 1000)
    assert asked[1][3]["Accept"] == "application/octet-stream" and asked[1][1] == "GET" and asked[1][2] is None
    with pytest.raises(InterruptedError):
        mod.download(rel["assets"]["Trimmer-1.3.0-setup.exe"], tmp_path / "c.exe", cancelled=lambda: True)
    assert not (tmp_path / "c.exe").exists()                           # no half file left
    with pytest.raises(ValueError):
        mod.download("https://example.com/evil.exe", tmp_path / "e.exe")
    assert len(asked) == 3                                             # the refused address was never asked


def window(monkeypatch, **settings):
    s = AppSettings()
    for k, v in settings.items():
        setattr(s, k, v)
    w = MainWindow(s)
    w.show()
    pump(0.1)
    return w


def test_first_launch_question(monkeypatch):
    monkeypatch.setattr(U, "install_kind", lambda *a, **k: "portable")
    checks = []
    monkeypatch.setattr(GU.Updater, "check", lambda self, manual=True: checks.append(manual))
    monkeypatch.setattr(GU, "LAUNCH_DELAY_MS", 0)
    monkeypatch.setattr(GU, "ask_permission", lambda parent: False)
    w = window(monkeypatch)
    w.updater.on_launch()
    pump(0.2)
    assert w.settings.update_checks == "off" and checks == []          # No: nothing connects
    w.updater.on_launch()                                              # and it doesn't ask again
    pump(0.2)
    assert checks == []
    w.settings.update_checks = "ask"
    monkeypatch.setattr(GU, "ask_permission", lambda parent: True)
    w.updater.on_launch()
    pump(0.2)
    assert w.settings.update_checks == "on" and checks == [False]
    w.close()


def test_installed_copy_does_not_ask(monkeypatch):
    monkeypatch.setattr(U, "install_kind", lambda *a, **k: "installed")
    monkeypatch.setattr(GU, "ask_permission", lambda parent: pytest.fail("the installer already asked"))
    checks = []
    monkeypatch.setattr(GU.Updater, "check", lambda self, manual=True: checks.append(manual))
    monkeypatch.setattr(GU, "LAUNCH_DELAY_MS", 0)
    w = window(monkeypatch)
    w.updater.on_launch()
    pump(0.2)
    assert checks == [False] and w.settings.update_checks == "on"
    w.close()


def release(tag="v9.0.0"):
    return {"tag": tag, "name": "", "notes": "- New things", "page": U.RELEASES_PAGE,
            "assets": {f"Trimmer-{tag[1:]}-setup.exe": "u", "SHA256SUMS.txt": "h"}}


@pytest.mark.parametrize("choice", ["never", "skip", "later"])
def test_popup_choices(monkeypatch, choice):
    shown = []

    def fake_exec(self):
        shown.append((self.windowTitle(), self.b_install.text(), self.findChildren(QLabel)))
        self.choice = choice
        return 1

    monkeypatch.setattr(GU.UpdateDialog, "exec", fake_exec)
    infos = []
    monkeypatch.setattr(GU.QMessageBox, "information", staticmethod(lambda *a, **k: infos.append(a[2])))
    w = window(monkeypatch, update_checks="on")
    w.updater._checked(release(), manual=False)
    assert shown and shown[0][0] == "Update available"
    texts = [lab.text() for lab in shown[0][2]]
    assert any("Settings > Updates" in t for t in texts) and any("9.0.0 is available" in t for t in texts)
    if choice == "never":
        assert w.settings.update_checks == "off"
    elif choice == "skip":
        assert w.settings.skipped_version == "9.0.0"
        w.updater._checked(release(), manual=False)                     # no popup for it again
        assert len(shown) == 1
        w.updater._checked(release(), manual=True)                      # Check now still shows it
        assert len(shown) == 2
        w.updater._checked(release("v9.0.1"), manual=False)             # the next version does
        assert len(shown) == 3
    else:
        assert w.settings.update_checks == "on" and not w.settings.skipped_version
    w.updater._checked(release("v0.1.0"), manual=True)
    assert infos and "latest version" in infos[-1]
    assert w.settings.last_update_check
    w.close()


def test_settings_page(monkeypatch, tmp_path):
    s = AppSettings(update_checks="off")
    d = SettingsDialog(s, ["software"])
    d.show_tab("Updates")
    assert not d.update_on.isChecked()
    d.update_on.setChecked(True)
    d._save()
    assert s.update_checks == "on"
    d.close()
    # Local features only: no switch, just what it means.
    monkeypatch.setattr(U, "available", lambda root=None: False)
    d = SettingsDialog(AppSettings(), ["software"])
    assert not hasattr(d, "update_on")
    page = [d.tabs.widget(i).widget() for i in range(d.tabs.count()) if d.tabs.tabText(i) == "Updates"][0]
    assert any("local features only" in lab.text() for lab in page.findChildren(QLabel))
    d._save()
    d.close()
    AppSettings().save()


class FakeUpdateFile:
    """Stands in for updater/trimmer_update.py: serves an installer and its checksums."""

    def __init__(self, installer=b"new installer", sums_for=None):
        self.installer = installer
        self.sums_for = installer if sums_for is None else sums_for
        self.fetched = []

    def download(self, url, dest, progress=None, cancelled=None, timeout=30.0):
        self.fetched.append(url)
        if url == "sums":
            Path(dest).write_text(f"{hashlib.sha256(self.sums_for).hexdigest()}  Trimmer-9.0.0-setup.exe\n")
        else:
            Path(dest).write_bytes(self.installer)
            if progress:
                progress(len(self.installer), len(self.installer))
        return dest


def install_window(monkeypatch, fake):
    rel = {"tag": "v9.0.0", "notes": "", "page": U.RELEASES_PAGE,
           "assets": {"Trimmer-9.0.0-setup.exe": "setup", "SHA256SUMS.txt": "sums"}}
    monkeypatch.setattr(U, "load_module", lambda root=None: fake)
    ran, quit_ = [], []
    monkeypatch.setattr(GU.subprocess, "Popen", lambda args, **k: ran.append(args))
    monkeypatch.setattr(GU.QApplication.instance(), "quit", lambda: quit_.append(True))
    warnings = []
    monkeypatch.setattr(GU.QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[2])))
    w = window(monkeypatch, update_checks="on")
    monkeypatch.setattr(w, "close", lambda: True)
    return w, rel, ran, quit_, warnings


def test_install_update_checks_the_download(monkeypatch):
    fake = FakeUpdateFile()
    w, rel, ran, quit_, warnings = install_window(monkeypatch, fake)
    w.updater.install(rel)
    pump(5, lambda: ran or warnings)
    assert not warnings and fake.fetched == ["sums", "setup"]
    assert ran[0][0].endswith("Trimmer-9.0.0-setup.exe") and "/SILENT" in ran[0] and "/RELAUNCH=1" in ran[0]
    assert quit_                                                   # Trimmer closes for the installer


def test_install_update_never_runs_a_bad_download(monkeypatch):
    fake = FakeUpdateFile(installer=b"tampered", sums_for=b"the real one")
    w, rel, ran, quit_, warnings = install_window(monkeypatch, fake)
    w.updater.install(rel)
    pump(5, lambda: ran or warnings)
    assert not ran and not quit_ and "checksum" in warnings[0]


def test_install_waits_for_the_queue(monkeypatch):
    fake = FakeUpdateFile()
    w, rel, ran, quit_, warnings = install_window(monkeypatch, fake)
    running = [True]
    monkeypatch.setattr(w.queue, "is_running", lambda: running[0])

    def later(self):
        next(b for b in self.buttons() if b.text().startswith("Install when")).click()
        return 0

    monkeypatch.setattr(GU.QMessageBox, "exec", later)
    w.updater.install(rel)
    pump(0.3)
    assert not fake.fetched and w.updater.pending is rel           # nothing yet
    running[0] = False
    w.queue.running_changed.emit(False)                            # the queue finished
    pump(5, lambda: ran)
    assert ran and fake.fetched == ["sums", "setup"]


def test_dont_ask_again_while_settings_are_open(monkeypatch):
    monkeypatch.setattr(GU.UpdateDialog, "exec", lambda self: setattr(self, "choice", "never") or 1)
    w = window(monkeypatch, update_checks="on")
    d = SettingsDialog(w.settings, ["software"], w)
    assert d.update_on.isChecked()
    w.updater._checked(release(), manual=True)                         # Check now, then Don't ask again
    assert not d.update_on.isChecked()
    d._save()                                                          # Save doesn't turn it back on
    assert w.settings.update_checks == "off"
    w.close()


def test_old_downloads_are_cleaned_up(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "download_folder", lambda: tmp_path)
    (tmp_path / "Trimmer-1.2.0-setup.exe").write_bytes(b"old")
    monkeypatch.setattr(GU, "ask_permission", lambda parent: False)
    monkeypatch.setattr(U, "install_kind", lambda *a, **k: "portable")
    w = window(monkeypatch)
    w.updater.on_launch()
    assert not list(tmp_path.iterdir())
    w.close()


def test_installer_reopens_trimmer_once():
    args = U.installer_args()
    assert "/RELAUNCH=1" in args and "/NORESTARTAPPLICATIONS" in args and "/SILENT" in args
    iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert "{param:RELAUNCH|0}" in iss and 'Excludes: "\\_internal\\updater"' in iss
    assert 'Tasks: updates' in iss and "Flags: exclusive unchecked" in iss
