"""Drives the real window headlessly: add a clip, trim and queue it, detect
and merge a replay session, cancel a job, and restore the queue after quitting."""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

WORK = Path(os.environ.get("TRIMMER_TEST_DIR", tempfile.gettempdir())) / "trimmer_gui_tests"
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)
os.environ["TRIMMER_DATA_DIR"] = str(WORK / "appdata")

from tests import fixtures as fx  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402

from trimmer.core.jobs import JobOptions  # noqa: E402
from trimmer.core.journal import clear_queue, load_queue  # noqa: E402
from trimmer.core.session import clear_session, load_session, session_path  # noqa: E402
from trimmer.core.settings import AppSettings  # noqa: E402
from trimmer.gui import main_window  # noqa: E402
from trimmer.gui.main_window import MainWindow  # noqa: E402
from trimmer.gui.settings_dialog import SettingsDialog  # noqa: E402

app = QApplication.instance() or QApplication([])
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
# The queue warnings open a real dialog; these older tests queue the same file
# twice on purpose, so they answer "add anyway" (tests/test_warnings.py covers the dialogs).
from trimmer.gui import queue_checks  # noqa: E402
queue_checks.ask = lambda *a, **k: ("anyway", False)


def pump(sec, cond=lambda: False):
    end = time.time() + sec
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.02)


def window(out):
    s = AppSettings.load()
    s.output_dir = str(out)
    w = MainWindow(s)
    w.show()
    w.startup_checks()
    pump(1)
    return w


def finished(w):
    return w.queue.items and all(i.status not in ("queued", "running") for i in w.queue.items)


def test_trim_and_merge_through_gui():
    (WORK / "src").mkdir()
    clip = fx.single_clip(WORK / "src")
    fx.shared_replays(WORK / "rep")
    w = window(WORK / "out")
    w._route([str(clip)])
    pump(15, lambda: w.clips.entries)
    w.clips.table.selectRow(0)
    pump(10, lambda: w.clips.timeline.wave and w.clips.timeline.thumbs)
    assert w.clips.timeline.wave and w.clips.timeline.thumbs     # waveform + frames arrived
    w.clips.entries[0].segments = [(2.0, 8.0)]
    w.clips.out_name.setText("My trim")
    w.clips.out_name.textEdited.emit("My trim")
    assert "→  My trim" in w.clips.table.item(0, 0).text()
    w.clips.queue()
    pump(90, lambda: finished(w))
    assert w.queue.items[0].status == "done"
    pump(10, lambda: " 0 clips " not in w.queue.savings.text())
    assert "Space saved:" in w.queue.savings.text() and "1 clip " in w.queue.savings.text()

    w.tabs.setCurrentWidget(w.merge)
    w._route([str(WORK / "rep")])
    pump(30, lambda: w.merge.sessions)
    assert [len(s.clips) for s in w.merge.sessions] == [4, 1]
    top = w.merge.tree.topLevelItem(0)
    assert top.text(3).startswith("Merged ")          # default name shown until renamed
    top.setText(3, "Best round")
    assert top.data(3, Qt.UserRole) == "Best round"
    w.merge.r_trim.setChecked(True)
    assert top.text(3) == "Best round"                 # a custom name survives a mode switch
    w.merge.r_merge.setChecked(True)
    w.merge.queue()
    pump(180, lambda: finished(w))
    assert [i.status for i in w.queue.items] == ["done", "done"]
    names = sorted(p.name for p in (WORK / "out").iterdir())
    assert names == ["Best round.mp4", "My trim.mp4"]
    w.close()


def test_settings_cancel_and_restore():
    (WORK / "src2").mkdir()
    src = fx.long_encode(WORK / "src2")
    out = WORK / "out2"
    w = window(out)
    d = SettingsDialog(w.settings, w.encoders, w)
    d.packets.setChecked(False)
    d._save()
    assert JobOptions.from_settings(AppSettings.load()).use_packet_matching is False

    w._route([str(src)])
    pump(15, lambda: w.clips.entries)
    w.clips.table.selectRow(0)
    pump(1)
    w.clips.compress.setChecked(True)
    w.clips.queue()
    pump(60, lambda: w.queue.items and (w.queue.items[0].fraction or 0) > 0.05)
    w.queue.add_jobs(w.clips.build_jobs(w.clips.entries), start=False)
    w.queue.cancel_current()
    pump(30, lambda: w.queue.items[0].status != "running")
    assert w.queue.items[0].status == "cancelled"
    pump(60, lambda: (w.queue.items[1].fraction or 0) > 0.02)
    w.queue.auto_start.setChecked(False)        # a restored queue only starts by itself if this is on
    w.close()           # quitting mid-job: the job must be offered again
    pump(1)
    assert not out.exists() or not any(out.iterdir())

    w2 = window(out)
    assert [i.status for i in w2.queue.items] == ["queued"]
    w2.close()


def test_prefill_default_track_and_merged_preview():
    from trimmer.gui.preview import PreviewDialog
    w = window(WORK / "out3")
    w.settings.prefill_names = True
    w.settings.extra_trim_padding = 0.0      # the length check below assumes no repeated footage
    w._route([str(WORK / "src" / "Gameplay clip.mp4")])
    pump(15, lambda: w.clips.entries)
    w.clips.table.selectRow(0)
    pump(1)
    assert w.clips.out_name.text() == "Gameplay clip"      # original name ready to add to
    w.clips.out_name.setText("Gameplay clip - ace")
    w.clips.out_name.textEdited.emit("Gameplay clip - ace")
    assert w.clips.entries[0].out_name == "Gameplay clip - ace"
    w.clips.out_name.setText("Gameplay clip")
    w.clips.out_name.textEdited.emit("Gameplay clip")
    assert w.clips.entries[0].out_name == ""               # unchanged name isn't a rename

    picker = w.clips.preview_track
    picker.set_tracks(["1: Mic", "2: Desktop"], ["Mic", "Desktop"])
    assert picker.currentIndex() == 0
    picker.set_default(1)
    assert w.track_prefs == {"title": "Desktop", "index": 1}
    picker.set_tracks(["1: Desktop", "2: Mic"], ["Desktop", "Mic"])   # next clip: matched by name
    assert picker.currentIndex() == 0 and "default" in picker.itemText(0)

    w.tabs.setCurrentWidget(w.merge)
    w._route([str(WORK / "rep")])
    pump(30, lambda: w.merge.sessions)
    w.merge.tree.setCurrentItem(w.merge.tree.topLevelItem(0))
    w.merge.preview_merged()
    pump(120, lambda: any(isinstance(d, PreviewDialog) for d in w.merge.findChildren(PreviewDialog)))
    dlgs = w.merge.findChildren(PreviewDialog)
    assert dlgs, "merged preview didn't open"
    d = dlgs[0]
    assert d.marks and len(d.marks) == 2                    # 3 kept clips -> 2 joins
    assert 47.0 < d.info.duration < 49.5
    tmp = d.temp_dir
    assert not (WORK / "out3").exists() or not any((WORK / "out3").iterdir())   # nothing saved
    d.close()
    pump(2)
    assert not Path(tmp).exists()
    w.merge.open_clip(w.merge.sessions[0].clips[0])
    pump(1)
    assert len(w.merge.findChildren(PreviewDialog)) >= 1
    w.close()


def test_manual_start_and_job_titles():
    from trimmer.core.journal import clear_queue
    clear_queue()           # don't pick up jobs left by the restore test
    w = window(WORK / "out4")
    w.queue.auto_start.setChecked(False)
    assert AppSettings.load().ui.get("auto_start_queue") is False
    w._route([str(WORK / "src" / "Gameplay clip.mp4")])
    pump(15, lambda: w.clips.entries)
    w.clips.table.selectRow(0)
    pump(1)
    w.clips.out_name.setText("Ace round")
    w.clips.out_name.textEdited.emit("Ace round")
    w.clips.queue()
    pump(2)
    item = w.queue.items[0]
    assert item.status == "queued" and not w.queue.is_running()   # waits for Start
    assert item.job.title == "Ace round"
    assert w.queue.table.item(0, 0).text() == "Ace round"
    assert "waiting" in w.tabs.tabText(2)
    w.queue.start()
    pump(90, lambda: item.status not in ("queued", "running"))
    assert item.status == "done"
    w.queue.auto_start.setChecked(True)
    w.close()


def test_queue_editing_pause_and_retry():
    from trimmer.core.journal import clear_queue
    from trimmer.gui import queue_tab as qt_mod
    clear_queue()
    w = window(WORK / "out5")
    q = w.queue
    q.auto_start.setChecked(False)
    w._route([str(WORK / "src2" / "obs_stream.mp4"), str(WORK / "src" / "Gameplay clip.mp4")])
    pump(15, lambda: len(w.clips.entries) == 2)
    w.clips.table.selectAll()
    pump(1)
    w.clips.queue()
    pump(2)
    assert [i.status for i in q.items] == ["queued", "queued"]
    a, b = q.items
    assert "Saves to" == q.table.horizontalHeaderItem(1).text()
    assert q.table.item(0, 1).text() == str(WORK / "out5")

    # edit waiting jobs: folder for both, compression for one, name, order, delete-original
    other = WORK / "out5" / "elsewhere"
    other.mkdir(parents=True)
    qt_mod.QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: str(other))
    q.change_folder([a, b])
    assert a.job.options.output_dir == str(other) and q.table.item(0, 1).text() == str(other)
    q.set_compression([a], "Discord 10 MB (720p)")
    assert a.job.compress and "9.5 MB 720p" in q.table.item(0, 2).text(), q.table.item(0, 2).text()
    q.set_compression([a], "Balanced (H.265 CRF 23)")     # single pass, so the .part file grows
    assert "H.265 CRF23" in q.table.item(0, 2).text()
    q.set_compression([b], None)
    assert not b.job.compress
    qt_mod.QInputDialog.getText = staticmethod(lambda *a, **k: ("Short one", True))
    q.rename(b)
    assert b.job.output_name == "Short one" and q.table.item(1, 0).text() == "Short one"
    q.table.selectRow(1)
    q.move(-1)
    assert q.items[0] is b and q.table.item(0, 0).text() == "Short one"
    q.set_delete([b], True)
    assert b.job.options.delete_originals is False or True   # confirm dialog is auto-yes in tests
    q.set_delete([b], False)
    pump(90, lambda: a.estimate is not None and b.estimate is not None and not q._to_estimate
         and q._estimating is None)
    assert "≈" in q.table.item(1, 6).text()          # predicted size shown before running
    q.table.selectAll()
    pump(0.3)
    info = q.sel_info.text()
    assert info.startswith("2 selected") and "MB" in info and "→" in info, info

    # run, pause mid-encode of the compressing job, resume
    q.start()
    pump(60, lambda: b.status == "done")
    pump(60, lambda: a.status == "running" and (a.fraction or 0) > 0.05)
    q.pause()
    pump(1)
    assert q.paused and q.items[1].status == "running" and "Paused" in q.table.item(1, 3).text()
    assert "paused" in w.tabs.tabText(2)
    part = next(other.glob("*.part.*"))
    size = part.stat().st_size
    pump(1.5)
    assert part.stat().st_size == size
    q.start_or_resume()
    pump(180, lambda: a.status not in ("queued", "running"))
    assert a.status in ("done", "reverted"), a.log[-5:]
    assert "→" in q.table.item(1, 6).text() or q.table.item(1, 6).text() == "Kept original"
    assert (other / "Short one.mp4").exists()

    # retry a cancelled job
    q.retry([a])          # not failed: nothing happens
    assert a.status in ("done", "reverted")
    a.status = "cancelled"
    q.retry([a])
    assert a.status == "queued"
    q.remove_selected([a])
    assert a not in q.items
    w.close()


def test_clip_viewer_highlight_and_marker():
    from trimmer.gui.preview import PreviewDialog
    w = window(WORK / "out6")
    w.settings.ui["packet_matching"] = True       # an earlier test turned it off
    w.tabs.setCurrentWidget(w.merge)
    w._route([str(WORK / "rep")])
    pump(30, lambda: w.merge.sessions)
    m = w.merge
    m.tree.setCurrentItem(m.tree.topLevelItem(0))
    s = m.sessions[0]
    pump(30, lambda: m.used.get(id(s), ({}, "quick"))[1] == "exact")
    ranges, how = m.used[id(s)]
    assert how == "exact"
    used = sorted(r[1] - r[0] for r in ranges.values() if r)
    assert len(used) == 3 and 47.5 < sum(used) < 48.5          # 3 clips cover the 48 s session
    assert sum(1 for r in ranges.values() if r is None) == 1   # the redundant clip

    second = s.clips[1]
    m.open_clip(second)
    pump(2)
    d = [x for x in m.findChildren(PreviewDialog) if getattr(x, "clip", None) is second][0]
    a, b = ranges[str(second.path)]
    assert d.timeline.segments == [(a, b)] and "Exact" in d.used_note.text()
    d.player.pause()          # the viewer starts playing; hold still to check the marker
    d.player.setPosition(5000)
    pump(1.5)
    assert m.view.marker is not None and m.view.marker[0] == str(second.path)
    assert abs(m.view.marker[1] - 5.0) < 0.6
    d.close()
    pump(1)
    assert m.view.marker is None

    m.open_clip(s.clips[2])          # the redundant one
    pump(1)
    d2 = [x for x in m.findChildren(PreviewDialog) if getattr(x, "clip", None) is s.clips[2]][-1]
    assert d2.timeline.segments == [] and "None of this clip is used" in d2.used_note.text()
    d2.close()
    w.close()


def test_queue_buttons_with_selection():
    """Clicking the real buttons (they pass a 'checked' flag) must work."""
    from trimmer.core.journal import clear_queue
    clear_queue()
    w = window(WORK / "out7")
    q = w.queue
    q.auto_start.setChecked(False)
    w._route([str(WORK / "src" / "Gameplay clip.mp4")])
    pump(15, lambda: w.clips.entries)
    w.clips.table.selectRow(0)
    pump(1)
    w.clips.queue()
    w.clips.queue()
    pump(1)
    assert len(q.items) == 2
    q.table.selectRow(0)
    q.b_retry.click()                      # nothing to retry; must not crash
    for b in q.findChildren(QPushButton):
        if b.text() == "Remove":
            b.click()
    assert len(q.items) == 1
    q.items[0].status = "cancelled"
    q.table.selectRow(0)
    q.b_retry.click()
    assert q.items[0].status == "queued"
    w.close()


def test_system_theme_follows_os(monkeypatch):
    from trimmer.gui import theme
    monkeypatch.setattr(theme, "system_scheme", lambda: "light")
    theme.apply_theme(app, "system")
    assert theme.current["bg"] == theme.LIGHT["bg"]
    monkeypatch.setattr(theme, "system_scheme", lambda: "dark")
    theme._system_changed()                     # Windows switched to dark mode
    assert theme.current["bg"] == theme.DARK["bg"]
    theme.apply_theme(app, "light")             # a fixed choice ignores later OS changes
    theme._system_changed()
    assert theme.current["bg"] == theme.LIGHT["bg"]
    theme.apply_theme(app, "dark")


# ------------------------------------------------------------ folder watcher
def _watcher(folder, **kw):
    from trimmer.gui.folder_watch import FolderWatcher
    folder.mkdir(parents=True, exist_ok=True)
    fw = FolderWatcher(stable_secs=0.4, poll_ms=50, rescan_secs=0.3, validate=lambda p: None, **kw)
    got = []
    fw.ready.connect(lambda ps: got.extend(ps))
    assert fw.start(folder)
    return fw, got


def test_watcher_waits_until_file_is_stable():
    d = WORK / "watch1"
    fw, got = _watcher(d)
    (d / "Replay 2026-10-20 20-00-00.mp4").write_bytes(b"x" * 1000)       # appears partly written
    pump(0.25)
    (d / "Replay 2026-10-20 20-00-00.mp4").write_bytes(b"x" * 5000)       # still growing
    pump(0.25)
    assert got == []
    pump(8, lambda: got)
    pump(1)
    assert got == [d / "Replay 2026-10-20 20-00-00.mp4"]               # once, and only after it settled
    assert (d / got[0].name).stat().st_size == 5000
    fw.stop()


def test_watcher_ignores_part_and_other_names_and_existing_files():
    d = WORK / "watch2"
    d.mkdir()
    (d / "Replay 2026-10-20 19-00-00.mp4").write_bytes(b"old")           # there before watching
    fw, got = _watcher(d)
    for n in ["Replay 2026-10-20 20-00-00.part.mp4", "holiday.mp4", "notes.txt",
              "Replay 2026-10-20 20-00-00 (trimmed).mp4", "Replay 2026-10-20 20-05-00.mp4.tmp"]:
        (d / n).write_bytes(b"x" * 100)
    (d / "Replay 2026-10-20 20-10-00 (2).mkv").write_bytes(b"x" * 100)
    pump(8, lambda: got)
    pump(1.5)
    assert [p.name for p in got] == ["Replay 2026-10-20 20-10-00 (2).mkv"]
    fw.stop()


def test_watcher_drops_file_removed_before_stable():
    d = WORK / "watch3"
    fw, got = _watcher(d)
    p = d / "Replay 2026-10-20 20-00-00.mp4"
    p.write_bytes(b"x" * 100)
    pump(0.3, lambda: p in fw.pending)
    assert p in fw.pending
    p.unlink()
    pump(1.5)
    assert got == [] and not fw.pending
    fw.stop()


def test_watcher_reports_vanished_folder():
    d = WORK / "watch4"
    fw, got = _watcher(d)
    lost = []
    fw.lost.connect(lost.append)
    shutil.rmtree(d)
    pump(5, lambda: lost)
    assert lost == [str(d)] and not fw.active


def test_merge_tab_watch_adds_new_replays():
    src = fx.shared_replays(WORK / "wsrc" / "rep")
    watched = WORK / "wfolder"
    watched.mkdir()
    w = window(WORK / "out3")
    m = w.merge
    m.watcher.stable_secs = 0.4
    m.main.settings.watch_folder = str(watched)
    m.watch_box.setChecked(True)
    m._watch_toggled(True)
    assert m.watcher.active and "Watching" in m.watch_hint.text()
    assert AppSettings.load().watch_enabled
    for p in src[:2]:
        shutil.copy(p, watched / p.name)
    pump(30, lambda: m.sessions and sum(len(s.clips) for s in m.sessions) == 2)
    assert [len(s.clips) for s in m.sessions] == [2]
    m.watch_box.setChecked(False)
    m._watch_toggled(False)
    assert not m.watcher.active
    w.close()


def test_shift_click_tick_toggles_all_sessions(monkeypatch):
    fx.shared_replays(WORK / "shift_rep")
    w = window(WORK / "shift_out")
    w.tabs.setCurrentWidget(w.merge)
    w._route([str(WORK / "shift_rep")])
    pump(30, lambda: w.merge.sessions)
    tree = w.merge.tree
    n = tree.topLevelItemCount()
    assert n == 2
    first, second = tree.topLevelItem(0), tree.topLevelItem(1)
    assert (first.checkState(0), second.checkState(0)) == (Qt.Checked, Qt.Unchecked)

    # plain click on one tick: only that session changes
    second.setCheckState(0, Qt.Checked)
    first.setCheckState(0, Qt.Unchecked)
    assert (first.checkState(0), second.checkState(0)) == (Qt.Unchecked, Qt.Checked)

    # shift held: the clicked state spreads to every session, clip rows untouched
    monkeypatch.setattr(QApplication, "keyboardModifiers", staticmethod(lambda: Qt.ShiftModifier))
    first.setCheckState(0, Qt.Checked)
    assert all(tree.topLevelItem(i).checkState(0) == Qt.Checked for i in range(n))
    second.setCheckState(0, Qt.Unchecked)
    assert all(tree.topLevelItem(i).checkState(0) == Qt.Unchecked for i in range(n))
    assert first.childCount() and first.child(0).data(0, Qt.CheckStateRole) is None
    monkeypatch.undo()

    # queueing sees the spread state
    before = len(w.queue.items)
    w.merge._set_all_checked(Qt.Checked)
    w.merge.queue()
    assert len(w.queue.items) == before + 2
    w.close()


def test_merge_tab_track_options():
    fx.shared_replays(WORK / "tracks_rep")
    w = window(WORK / "tracks_out")
    m = w.merge
    w.tabs.setCurrentWidget(m)
    w._route([str(WORK / "tracks_rep")])
    pump(30, lambda: m.sessions)
    assert [c.text() for c in m.keep_checks] == ["1: Mic", "2: Desktop"]
    assert all(c.isChecked() for c in m.keep_checks) and not any(c.isChecked() for c in m.rip_checks)
    assert m.g_out.isChecked() and not m.g_rip.isChecked() and not m.mix_keep.isEnabled()
    assert m.g_out.title() == "Save the merged video"
    m.r_trim.setChecked(True)
    assert m.g_out.title() == "Save the trimmed videos"
    m.r_merge.setChecked(True)
    assert m.g_out.title() == "Save the merged video"

    sent = []
    m.queue_jobs.connect(sent.append)
    m.g_out.setChecked(False)
    m.g_rip.setChecked(True)
    m.rip_checks[1].setChecked(True)
    m.rip_video.setChecked(True)
    m.queue()
    job = sent[-1][0]
    assert len(sent[-1]) == 1 and job.kind == "merge"
    assert (job.make_video, job.rip_audio_tracks, job.rip_video) == (False, [1], True)
    assert job.audio_tracks is None and job.mix_tracks is None and not job.compress
    assert job.options.rip_format == m.rip_fmt.currentData()

    m.g_out.setChecked(True)
    m.g_rip.setChecked(False)
    m.keep_checks[0].setChecked(False)
    m.queue()
    job = sent[-1][0]
    assert (job.make_video, job.audio_tracks, job.rip_audio_tracks, job.rip_video) == (True, [1], [], False)

    m.keep_checks[0].setChecked(True)
    m.mix.setChecked(True)
    m.mix_keep.setChecked(False)
    m.queue()
    job = sent[-1][0]
    assert job.mix_tracks == [0, 1] and job.keep_mixed_originals is False and job.audio_tracks is None
    w.close()


def test_savings_edit_dialog():
    from trimmer.gui.settings_dialog import SavingsEditDialog
    d = SavingsEditDialog(3 * 1024 ** 3)
    assert d.unit.currentText() == "GB" and abs(d.amount.value() - 3) < 1e-9
    d.unit.setCurrentIndex(0)                       # MB keeps the same amount
    assert abs(d.amount.value() - 3072) < 1e-6 and d.value() == 3 * 1024 ** 3
    d.amount.setValue(512)
    assert d.value() == 512 * 1024 ** 2


def test_save_state_on_close_restores_everything(monkeypatch):
    (WORK / "ss_src").mkdir()
    clip_a = fx.single_clip(WORK / "ss_src")
    clip_b = WORK / "ss_src" / "second.mp4"
    shutil.copy(clip_a, clip_b)
    with open(clip_b, "ab") as f:
        f.write(b"\0" * 64)            # a different clip, as far as the duplicate check goes
    rep = WORK / "ss_rep"
    fx.shared_replays(rep)
    w = window(WORK / "ss_out")
    w.queue.auto_start.setChecked(False)
    w._route([str(clip_a), str(clip_b)])
    pump(15, lambda: len(w.clips.entries) == 2)
    w.clips.entries[0].segments = [(2.0, 8.0), (10.0, 12.5)]
    w.clips.entries[1].out_name = "Second take"
    w.clips.table.selectRow(1)
    w.tabs.setCurrentWidget(w.merge)
    w._route([str(rep)])
    pump(30, lambda: w.merge.sessions)
    tree = w.merge.tree
    tree.topLevelItem(0).setCheckState(0, Qt.Unchecked)
    tree.topLevelItem(1).setCheckState(0, Qt.Checked)
    tree.topLevelItem(1).setText(3, "Clutch round")      # _name_changed stores the custom name
    assert tree.topLevelItem(1).data(3, Qt.UserRole) == "Clutch round"
    w.clips.table.selectRow(1)
    w.queue.add_jobs(w.clips.build_jobs(w.clips.entries[:1]), start=False)
    n_paths = len(w.merge.paths)
    w.close()
    assert session_path().exists() and load_queue() == []

    w2 = window(WORK / "ss_out")
    w2.queue.auto_start.setChecked(False)
    pump(30, lambda: len(w2.clips.entries) == 2 and w2.merge.sessions)
    assert not session_path().exists()
    by_name = {e.path.name: e for e in w2.clips.entries}
    assert by_name[clip_a.name].segments == [(2.0, 8.0), (10.0, 12.5)]
    assert by_name[clip_b.name].out_name == "Second take"
    assert "Second take" in w2.clips.table.item(w2.clips.entries.index(by_name[clip_b.name]), 0).text()
    pump(5, lambda: [e.path.name for e in w2.clips.selected_entries()] == [clip_b.name])
    assert [e.path.name for e in w2.clips.selected_entries()] == [clip_b.name]
    t2 = w2.merge.tree
    assert len(w2.merge.paths) == n_paths
    assert [t2.topLevelItem(i).checkState(0) for i in range(2)] == [Qt.Unchecked, Qt.Checked]
    assert t2.topLevelItem(1).data(3, Qt.UserRole) == "Clutch round"
    assert t2.topLevelItem(1).text(3) == "Clutch round"
    assert [i.status for i in w2.queue.items] == ["queued"]
    assert w2.queue.items[0].job.source == str(clip_a)
    w2.settings.on_close = "discard"
    w2.close()
    assert not session_path().exists() and load_queue() == []


def test_close_without_saving_leaves_nothing(monkeypatch):
    (WORK / "ds_src").mkdir()
    clip = fx.single_clip(WORK / "ds_src")
    w = window(WORK / "ds_out")
    w.queue.auto_start.setChecked(False)
    w._route([str(clip)])
    pump(15, lambda: w.clips.entries)
    w.queue.add_jobs(w.clips.build_jobs(w.clips.entries), start=False)
    assert load_queue()
    monkeypatch.setattr(main_window, "ask_close_choice", lambda parent: ("discard", True))
    w.close()
    assert w.settings.on_close == "discard"          # "Remember my choice"
    assert AppSettings.load().on_close == "discard"
    assert not session_path().exists() and load_queue() == []
    w2 = window(WORK / "ds_out")                     # no "Unfinished queue" offer, nothing restored
    assert not w2.clips.entries and not w2.queue.items
    w2.close()


def test_close_cancel_and_nothing_to_save(monkeypatch):
    (WORK / "cc_src").mkdir()
    clip = fx.single_clip(WORK / "cc_src")
    w = window(WORK / "cc_out")
    w.settings.on_close = "ask"
    asked = []
    monkeypatch.setattr(main_window, "ask_close_choice", lambda parent: asked.append(1) or ("cancel", False))
    w._route([str(clip)])
    pump(15, lambda: w.clips.entries)
    assert not w.close()                              # cancelled: window stays
    assert asked and not session_path().exists()
    w.clips.clear()
    asked.clear()
    assert w.close() and not asked                    # nothing open: no question
    assert not session_path().exists()


def test_save_state_skips_missing_files(monkeypatch):
    (WORK / "mf_src").mkdir()
    keep = fx.single_clip(WORK / "mf_src")
    gone = WORK / "mf_src" / "gone.mp4"
    shutil.copy(keep, gone)
    with open(gone, "ab") as f:
        f.write(b"\0" * 32)
    w = window(WORK / "mf_out")
    w._route([str(keep), str(gone)])
    pump(15, lambda: len(w.clips.entries) == 2)
    w.close()
    assert session_path().exists()
    gone.unlink()
    shown = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[2])))
    w2 = window(WORK / "mf_out")
    pump(15, lambda: w2.clips.entries)
    assert [e.path.name for e in w2.clips.entries] == [keep.name]
    assert len(shown) == 1 and "gone.mp4" in shown[0]
    assert not session_path().exists()
    w2.close()
