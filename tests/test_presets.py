"""Organising compression presets in Settings > Compression: order, rename, duplicate, update,
hide from the lists, and asking before a save replaces one. Then the lists that offer them."""

# First: this sets up the offscreen app and the tests' own data folder.
from tests.gui_helpers import WORK, app, gameplay_clip, window  # noqa: F401
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMenu, QMessageBox

from trimmer.core.describe import preset_summary
from trimmer.core.encode import EncodeSettings
from trimmer.core.jobs import ClipJob, JobOptions
from trimmer.core.settings import AUTO3_PRESET, AppSettings, default_presets
from trimmer.gui import queue_tab
from trimmer.gui import settings_dialog as SD
from trimmer.gui.clips_tab import ClipEntry
from trimmer.gui.widgets import CUSTOM_PRESET, copy_opts
from tests.test_lists import fake_clip

BALANCED = "Balanced (H.265 CRF 23)"
DISCORD = "Discord 10 MB (720p)"


def listed(d):
    return [d.plist.item(i).text() for i in range(d.plist.count())]


def pick(d, name):
    d.plist.setCurrentRow(listed(d).index(name))


def answers(monkeypatch, names=(), yes=True):
    """Name boxes answer `names` in turn; every Yes / Cancel question answers `yes`."""
    names, asked = list(names), []
    monkeypatch.setattr(SD.QInputDialog, "getText", lambda *a, **k: (names.pop(0), True) if names else ("", False))
    monkeypatch.setattr(SD.QMessageBox, "question",
                        lambda _p, _t, text, *a: asked.append(text) or (QMessageBox.Yes if yes else QMessageBox.Cancel))
    monkeypatch.setattr(SD.QMessageBox, "warning", lambda _p, _t, text: asked.append(text))
    return asked


def menu(d):
    """The presets list's right-click menu, as {text: action} (the key hint left off)."""
    return {a.text().split("\t")[0]: a for a in d._preset_menu().actions() if a.text()}


def combo_items(combo):
    return [combo.itemText(i) for i in range(combo.count())]


def test_settings_keep_order_and_hidden_presets(tmp_path, monkeypatch):
    from trimmer.core import settings as st
    monkeypatch.setattr(st, "app_data_dir", lambda: tmp_path)
    s = AppSettings()
    s.presets = {"B": {}, "A": {}, "C": {}}
    s.hidden_presets = ["C", "gone", 3, "C"]
    s.save()
    got = AppSettings.load()
    assert list(got.presets)[:3] == ["B", "A", "C"]                 # the order is kept
    assert got.hidden_presets == ["C"]                              # names that exist, once each
    assert got.shown_presets()[:2] == ["B", "A"] and "C" not in got.shown_presets()
    (tmp_path / "settings.json").write_text('{"hidden_presets": "C", "ui": {"preset_auto3_added": true}}')
    assert AppSettings.load().hidden_presets == []                  # a hand-edited mistake


def test_preset_summary():
    p = default_presets()
    assert preset_summary(p[AUTO3_PRESET]) == ("H.265, Auto 3 (target 93), preset medium, audio copied, "
                                               "keep original if larger")
    assert preset_summary(p[DISCORD]).startswith("H.264, target size 9.5 MB, preset medium, audio 96 kbps, max 720p")
    assert preset_summary(p["Lossless (H.264)"]).startswith("H.264, lossless")
    nv = EncodeSettings(codec="h265", encoder="nvenc", quality=28).to_dict()
    assert preset_summary(nv).startswith("H.265 (NVENC), CQ 28")


def test_organise_presets_in_settings(monkeypatch):
    s = AppSettings()
    d = SD.SettingsDialog(s, ["software"])
    start = listed(d)
    assert start == list(default_presets())
    assert all(d.plist.item(i).checkState() == Qt.Checked for i in range(d.plist.count()))
    assert not menu(d)["Rename…"].isEnabled() and not d.b_load.isEnabled()  # nothing picked yet
    assert menu(d)["Save custom as preset…"].isEnabled()
    asked = answers(monkeypatch, ["Mine", "My balanced", "Mine", CUSTOM_PRESET, "Other"])

    # Move: from the right-click menu, and the ends stop.
    pick(d, BALANCED)
    menu(d)["Move up"].trigger()
    assert listed(d).index(BALANCED) == start.index(BALANCED) - 1 and d._current_preset() == BALANCED
    pick(d, start[0])
    assert not menu(d)["Move up"].isEnabled() and menu(d)["Move down"].isEnabled()
    pick(d, start[-1])
    assert not menu(d)["Move down"].isEnabled()

    # Save as a new preset: goes at the end and is picked.
    d.enc.set_settings(EncodeSettings(codec="h264", quality=30))
    d._save_preset()
    assert listed(d)[-1] == "Mine" and d._current_preset() == "Mine"
    assert d.presets["Mine"]["quality"] == 30

    # Rename keeps its place and settings, and remembers the old name.
    pick(d, BALANCED)
    where = listed(d).index(BALANCED)
    d._rename_preset()
    assert listed(d)[where] == "My balanced" and BALANCED not in d.presets
    assert d.renamed == {BALANCED: "My balanced"}
    # A name that's taken, or the custom settings' name, isn't used.
    d._rename_preset()                                              # "Mine": taken
    assert "already a preset" in asked[-1] and "My balanced" in d.presets
    d._rename_preset()                                              # custom's name, then "Other"
    assert CUSTOM_PRESET in asked[-1] and listed(d)[where] == "Other"
    assert d.renamed == {BALANCED: "Other"}                         # still from the first name

    # Duplicate goes right after the original.
    d._dup_preset()
    assert listed(d)[where + 1] == "Other (copy)" and d.presets["Other (copy)"] == d.presets["Other"]
    pick(d, "Other")
    d._dup_preset()
    assert listed(d)[where + 1] == "Other (copy 2)"

    # Update from custom asks first.
    pick(d, "Other")
    d.enc.set_settings(EncodeSettings(codec="h264", quality=40))
    d._update_preset()
    assert "Replace the settings of \"Other\"" in asked[-1] and d.presets["Other"]["quality"] == 40

    # Untick: hidden from the lists, kept in Settings. The menu's tick does the same.
    d.plist.item(listed(d).index(DISCORD)).setCheckState(Qt.Unchecked)
    assert DISCORD in d.hidden
    pick(d, DISCORD)
    shown = menu(d)["Show in the Compress lists"]
    assert not shown.isChecked()
    shown.trigger()
    assert DISCORD not in d.hidden
    menu(d)["Show in the Compress lists"].trigger()
    assert DISCORD in d.hidden
    # Delete picks the next one.
    pick(d, "Other (copy 2)")
    menu(d)["Delete"].trigger()
    assert "Other (copy 2)" not in listed(d) and d._current_preset() == "Other (copy)"

    d._save()
    assert list(s.presets) == listed(d)
    assert s.hidden_presets == [DISCORD]
    assert DISCORD not in s.shown_presets() and DISCORD in s.presets
    again = SD.SettingsDialog(s, ["software"])
    assert listed(again) == listed(d)
    assert again.plist.item(listed(again).index(DISCORD)).checkState() == Qt.Unchecked
    assert "Hidden" in again.plist.item(listed(again).index(DISCORD)).toolTip()
    again._reset_presets()
    assert listed(again) == list(default_presets()) and not again.hidden


def test_saving_over_a_preset_asks(monkeypatch):
    d = SD.SettingsDialog(AppSettings(), ["software"])
    asked = answers(monkeypatch, [BALANCED], yes=False)
    before = dict(d.presets[BALANCED])
    d.enc.set_settings(EncodeSettings(codec="h264", quality=35))
    d._save_preset()
    assert "already a preset" in asked[-1] and d.presets[BALANCED] == before     # Cancel: untouched
    answers(monkeypatch, [BALANCED], yes=True)
    where = listed(d).index(BALANCED)
    d._save_preset()
    assert d.presets[BALANCED]["quality"] == 35 and listed(d).index(BALANCED) == where


def test_drag_reorders(monkeypatch):
    d = SD.SettingsDialog(AppSettings(), ["software"])
    first = listed(d)[0]
    it = d.plist.takeItem(0)            # what a drag does: the row moves
    d.plist.insertItem(2, it)
    d._preset_order_from_list()
    assert listed(d)[2] == first and list(d.presets) == listed(d)


def test_lists_leave_out_hidden_presets_but_keep_ones_in_use(monkeypatch):
    w = window(WORK / "out_presets")
    w.settings.hidden_presets = [DISCORD]
    w.clips.refresh_from_settings()
    w.merge.refresh_from_settings()
    for combo in (w.clips.preset, w.merge.preset):
        items = combo_items(combo)
        assert items[0] == CUSTOM_PRESET and DISCORD not in items and items[1:] == w.settings.shown_presets()
        assert combo.itemData(1, Qt.ToolTipRole) == preset_summary(w.settings.presets[items[1]])
    tab = w.clips
    a, b = fake_clip("A.mp4", 10, 1_000), fake_clip("B.mp4", 10, 1_000)
    a.opts = copy_opts(tab.shared)
    a.opts.update(compress=True, preset=DISCORD)                    # set before it was hidden
    for e in (a, b):
        tab.entries.append(e)
        tab._add_row(e)
    tab.table.selectRow(0)
    assert tab.preset.currentText() == DISCORD                      # shown, not as Custom
    assert "Hidden in Settings" in tab.preset.itemData(tab.preset.currentIndex(), Qt.ToolTipRole)
    assert tab.encode_settings(DISCORD).target_size_mb == 9.5
    tab.table.selectRow(1)
    assert DISCORD not in combo_items(tab.preset)                   # gone again once nothing uses it

    # The Queue's right-click menu leaves it out too.
    seen = []

    class Menu(QMenu):
        def exec(self, *a):
            comp = next(x.menu() for x in self.actions() if x.menu() and x.text() == "Compression")
            seen.extend(x.text() for x in comp.actions())
    monkeypatch.setattr(queue_tab, "QMenu", Menu)
    w.queue.add_jobs([ClipJob(str(gameplay_clip()), segments=[(0, 2)], output_name="Q",
                              options=JobOptions(output_dir=str(WORK)))], start=False)
    w.queue.table.selectRow(0)
    w.queue._menu(w.queue.table.rect().center())
    assert BALANCED in seen and DISCORD not in seen
    tab.clear()
    w.close()


def test_renaming_a_preset_follows_into_clips_and_merges(monkeypatch):
    w = window(WORK / "out_presets_rename")
    tab = w.clips
    e = fake_clip("A.mp4", 10, 1_000)
    e.opts = copy_opts(tab.shared)
    e.opts.update(compress=True, preset=BALANCED)
    tab.entries.append(e)
    tab._add_row(e)
    tab.shared["preset"] = BALANCED
    w.merge.shared["preset"] = BALANCED

    def run(d):
        answers(monkeypatch, ["Everyday"])
        pick(d, BALANCED)
        d._rename_preset()
        d._save()
        return 1
    monkeypatch.setattr(SD.SettingsDialog, "exec", run)
    w.open_settings()
    assert "Everyday" in w.settings.presets and BALANCED not in w.settings.presets
    assert e.opts["preset"] == "Everyday" and tab.shared["preset"] == "Everyday"
    assert w.merge.shared["preset"] == "Everyday"
    tab.table.selectRow(0)
    assert tab.preset.currentText() == "Everyday"
    assert isinstance(e, ClipEntry)
    tab.clear()
    w.close()


def test_preset_keys(monkeypatch):
    from PySide6.QtTest import QTest
    d = SD.SettingsDialog(AppSettings(), ["software"])
    d.tabs.setCurrentIndex(1)
    d.show()
    d.activateWindow()
    QTest.qWaitForWindowActive(d)
    answers(monkeypatch, ["Keyed"])
    pick(d, BALANCED)
    d.plist.setFocus()
    where = listed(d).index(BALANCED)
    QTest.keyClick(d.plist, Qt.Key_Up, Qt.AltModifier)
    assert listed(d).index(BALANCED) == where - 1
    QTest.keyClick(d.plist, Qt.Key_D, Qt.ControlModifier)
    assert listed(d)[where] == BALANCED + " (copy)"
    QTest.keyClick(d.plist, Qt.Key_F2)
    assert listed(d)[where] == "Keyed"
    QTest.keyClick(d.plist, Qt.Key_Delete)
    assert "Keyed" not in listed(d)
    d.enc.set_settings(EncodeSettings(codec="h264", quality=40))
    pick(d, BALANCED)
    QTest.keyClick(d.plist, Qt.Key_Return)
    assert d.enc.settings().to_dict() == EncodeSettings.from_dict(d.presets[BALANCED]).to_dict()
    assert d.isVisible()                                            # Enter loaded it, didn't close Settings
    d.close()
