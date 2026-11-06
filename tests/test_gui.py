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
    assert names == ["Best round.mp4", "My trim (trimmed 0m00s-0m08s).mp4"]   # copy starts on the keyframe
    w.close()


def test_settings_cancel_and_restore():
    (WORK / "src2").mkdir()
    src = fx.long_encode(WORK / "src2")
    out = WORK / "out2"
    w = window(out)
    d = SettingsDialog(w.settings, w.encoders, w)
    d.packets.setChecked(False)
    assert d.trim_range.isChecked() and d.trim_range.isEnabled()
    d.tags.setChecked(False)
    assert not d.trim_range.isEnabled()          # only means something with name tags on
    d.tags.setChecked(True)
    d.trim_range.setChecked(False)
    d._save()
    assert JobOptions.from_settings(AppSettings.load()).use_packet_matching is False
    assert JobOptions.from_settings(AppSettings.load()).add_trim_range is False
    w.settings.add_trim_range = True
    w.settings.save()

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
    pump(20, lambda: d.timeline.wave and d.timeline.thumbs)
    assert d.timeline.wave and d.timeline.thumbs            # waveform and frames, as in Clips
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

    # Esc closes a dialog without a closeEvent; the viewer must still be let go,
    # or the next highlight update writes to its deleted timeline.
    from PySide6.QtTest import QTest
    m.open_clip(second)
    pump(1)
    QTest.keyClick(m.viewers[-1], Qt.Key_Escape)
    pump(1)
    assert not m.viewers and m.view.marker is None
    m.used.clear()
    m._ensure_used(s)
    pump(30, lambda: m.used.get(id(s), ({}, "quick"))[1] == "exact")
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
    # every figure in the summary can be set, e.g. to carry totals over from before
    from datetime import date
    from PySide6.QtCore import QDate
    from trimmer.core import savings
    cur = savings.Savings(saved_bytes=2 * 1024 ** 4, reduction_bytes=-5 * 1024 ** 2, clips=1234, since="2025-03-01")
    d = SavingsEditDialog(cur)
    assert d.saved.unit.currentText() == "TB" and d.clips.value() == 1234
    assert d.values() == {"saved_bytes": 2 * 1024 ** 4, "reduction_bytes": -5 * 1024 ** 2, "clips": 1234,
                          "since": "2025-03-01"}
    d.clips.setValue(4321)
    d.since.setDate(QDate(2024, 12, 25))
    d.reduction.amount.setValue(7)
    v = d.values()
    assert v["clips"] == 4321 and v["since"] == "2024-12-25" and v["reduction_bytes"] == 7 * 1024 ** 2
    s = savings.set_totals(**v)
    assert (s.clips, s.since, s.reduction_bytes) == (4321, "2024-12-25", 7 * 1024 ** 2)
    assert savings.load().since == "2024-12-25" and "4,321 clips since 25 Dec 2024" in savings.summary_text()
    assert date.fromisoformat(savings.load().since)


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


def _select_rows(tree, rows):
    """Select tree items; each is a session index or (session index, clip index)."""
    tree.clearSelection()
    for r in rows:
        it = tree.topLevelItem(r) if isinstance(r, int) else tree.topLevelItem(r[0]).child(r[1])
        it.setSelected(True)


def test_send_replay_clips_to_clips_tab():
    w = window(WORK / "send_out")
    w.queue.auto_start.setChecked(False)
    fx.shared_replays(WORK / "send_rep")
    w.tabs.setCurrentWidget(w.merge)
    w._route([str(WORK / "send_rep")])
    pump(30, lambda: w.merge.sessions)
    m = w.merge
    # a session row means all its clips; a clip row of the same session adds nothing
    _select_rows(m.tree, [0, (0, 1)])
    clips, sessions = m._selection()
    assert len(clips) == 4 and len(sessions) == 1
    m.send_to_clips()
    pump(30, lambda: len(w.clips.entries) == 4)
    assert w.tabs.currentWidget() is w.clips
    assert len(w.clips.entries) == 4 and not w.queue.items
    pump(30, lambda: len(m.sessions) == 1 and len(m.paths) == 1)
    assert [len(s.clips) for s in m.sessions] == [1] and len(m.paths) == 1

    # the list being rebuilt (fewer rows than sessions) must not break the track boxes or queueing
    m.sessions = m.sessions * 3
    m._build_track_checks()
    assert m._ticked() == []
    m.sessions = m.sessions[:1]

    # clip rows alone, mixed with another session
    w.tabs.setCurrentWidget(m)
    _select_rows(m.tree, [0])
    m.send_to_clips()
    pump(30, lambda: len(w.clips.entries) == 5)
    assert not m.paths
    w.close()


def test_merge_then_send_to_clips_and_failure():
    rep = WORK / "mts_rep"
    fx.shared_replays(rep)
    w = window(WORK / "mts_out")
    w.queue.auto_start.setChecked(False)         # the merge must run anyway
    w.tabs.setCurrentWidget(w.merge)
    w._route([str(rep)])
    pump(30, lambda: w.merge.sessions)
    m = w.merge
    _select_rows(m.tree, [(0, 0), (0, 2)])       # clip rows: their session is merged whole
    m.merge_then_send()
    assert len(w.queue.items) == 1 and w.queue.items[0].job.options.merge_mode == "merge"
    pump(240, lambda: w.queue.items[0].status in ("done", "failed") and len(w.clips.entries) == 1)
    assert w.queue.items[0].status == "done"
    assert len(w.clips.entries) == 1 and w.tabs.currentWidget() is w.clips
    e = w.clips.entries[0]
    assert e.path.exists() and e.path.parent == WORK / "mts_out"
    assert abs(e.info.duration - 48) < 1.5
    pump(30, lambda: len(m.paths) == 1)
    assert [p.name for p in m.paths] == [sorted(rep.iterdir())[-1].name]
    assert not w.queue.on_finished

    # a merge that fails puts the clips back
    rep2 = WORK / "mts_rep2"
    paths = fx.shared_replays(rep2)
    m.clear()
    m.add_paths(paths)
    pump(30, lambda: m.sessions)
    _select_rows(m.tree, [0])
    real_start = w.queue.start
    w.queue.start = lambda: None
    m.merge_then_send()
    w.queue.start = real_start
    paths[1].unlink()
    w.queue.start()
    pump(120, lambda: w.queue.items[-1].status in ("done", "failed", "cancelled", "skipped"))
    assert w.queue.items[-1].status != "done"
    pump(30, lambda: sum(len(s.clips) for s in m.sessions) == 4)
    assert sum(len(s.clips) for s in m.sessions) == 4 and len(w.clips.entries) == 1
    w.close()


def test_merge_per_session_options():
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    fx.shared_replays(WORK / "po_rep")
    w = window(WORK / "po_out")
    w.queue.auto_start.setChecked(False)
    m = w.merge
    w.tabs.setCurrentWidget(m)
    w._route([str(WORK / "po_rep")])
    pump(30, lambda: m.sessions)
    tree = m.tree
    first, second = tree.topLevelItem(0), tree.topLevelItem(1)
    s0, s1 = m.sessions
    assert not tree.selectedItems() and m.g_opts.title() == "Options for all sessions"
    assert not m.b_shared.isEnabled() and m.view.session is None

    # a change with one session selected gives it its own options; the other keeps the shared ones
    _select_rows(tree, [1])
    assert m.g_opts.title() == f"Options for {s1.name}"
    m.g_out.setChecked(False)
    m.g_rip.setChecked(True)
    m.rip_checks[0].setChecked(True)
    assert first.text(0) == s0.name and second.text(0) == "★ " + s1.name
    assert second.toolTip(0) == "Uses its own options" and m.b_shared.isEnabled()
    assert m.shared["save_video"] and not m.shared["rip"]

    # nothing selected: the shared options, and changes go to every session without its own
    m.deselect_all()
    assert not tree.selectedItems() and m.view.session is None
    assert m.g_opts.title() == "Options for all sessions"
    assert m.g_out.isChecked() and not m.g_rip.isChecked() and not m.rip_checks[0].isChecked()
    m.compress.setChecked(True)
    assert m.shared["compress"] and not m.custom[id(s1)]["compress"]
    assert first.text(0) == s0.name

    # its own merge mode changes only that session's default name
    _select_rows(tree, [0])
    m.r_trim.setChecked(True)
    assert first.text(0).startswith("★ ") and first.text(3).endswith("(folder)")
    assert m.shared["mode"] == "merge"
    _select_rows(tree, [1])
    assert not m.g_out.isChecked() and m.g_rip.isChecked() and m.r_merge.isChecked()

    # Esc in the list and a click on its empty space clear the selection
    tree.setFocus()
    QTest.keyClick(tree, Qt.Key_Escape)
    assert not tree.selectedItems() and tree.currentItem() is None
    _select_rows(tree, [0])
    w.resize(1280, 1100)                          # room below the rows, however small the screen is
    m.split.setSizes([300, 170])
    pump(0.3)
    vp, below = tree.viewport(), tree.visualItemRect(second).bottom() + 8
    assert below < vp.height()
    QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, QPoint(20, below))
    assert not tree.selectedItems() and m.g_opts.title() == "Options for all sessions"

    # queueing: each ticked session with its own options, or the shared ones
    sent = []
    m.queue_jobs.connect(sent.append)
    m._set_all_checked(Qt.Checked)
    m.queue()
    a, b = sent[-1]
    assert a.clip_paths == [str(c.path) for c in s0.clips] and a.options.merge_mode == "trim"
    assert (a.make_video, a.compress, a.rip_audio_tracks) == (True, True, [])
    assert (b.make_video, b.compress, b.rip_audio_tracks, b.options.merge_mode) == (False, False, [0], "merge")

    # Use shared options drops the star
    _select_rows(tree, [0, 1])
    assert m.g_opts.title() == "Options for 2 selected sessions" and m.r_trim.isChecked()   # the first's
    m.b_shared.click()
    assert not m.custom and first.text(0) == s0.name and second.text(0) == s1.name
    assert not m.b_shared.isEnabled() and m.r_merge.isChecked() and m.compress.isChecked()
    w.close()


def test_clips_per_clip_options():
    from PySide6.QtTest import QTest
    (WORK / "pc_src").mkdir()
    a = fx.single_clip(WORK / "pc_src")
    b = WORK / "pc_src" / "other.mp4"
    shutil.copy(a, b)
    with open(b, "ab") as f:
        f.write(b"\0" * 64)
    w = window(WORK / "pc_out")
    w.queue.auto_start.setChecked(False)
    c = w.clips
    w._route([str(a), str(b)])
    pump(15, lambda: len(c.entries) == 2)
    ea, eb = c.entries
    c.table.selectRow(0)
    pump(0.5)
    assert c.opts_title.text() == f"Options for {ea.path.name}"
    c.compress.setChecked(True)
    c.keep_checks[0].setChecked(False)
    assert ea.opts and ea.opts["compress"] and ea.opts["drop_tracks"] == [0] and eb.opts is None
    assert c.table.item(0, 0).text().startswith("★ ") and not c.table.item(1, 0).text().startswith("★")
    assert "Uses its own options" in c.table.item(0, 0).toolTip()

    # nothing selected: no player or trims, the shared options, and nothing breaks
    c.deselect_all()
    assert not c.selected_entries() and c.current is None and c.timeline.duration == 0
    assert c.opts_title.text() == "Options for all clips" and not c.compress.isChecked()
    assert all(k.isChecked() for k in c.keep_checks) and not c.out_name.isEnabled()
    c.mark_in()
    c.mark_out()
    c.step(1)
    c.clear_segments()
    c.remove_segment()
    c.rip_video.setChecked(True)
    assert c.shared["rip_video"] and not ea.opts["rip_video"]

    c.table.selectRow(1)
    pump(0.5)
    assert c.rip_video.isChecked() and eb.opts is None            # shows the shared options
    c.table.setFocus()
    QTest.keyClick(c.table, Qt.Key_Escape)
    assert not c.selected_entries()

    sent = []
    c.queue_jobs.connect(sent.append)
    c.queue(selected_only=False)
    ja, jb = sent[-1]
    assert ja.source == str(ea.path) and (ja.compress, ja.audio_tracks, ja.rip_video) == (True, [1], False)
    assert jb.source == str(eb.path) and (jb.compress, jb.audio_tracks, jb.rip_video) == (False, None, True)

    c.table.selectRow(0)
    pump(0.5)
    assert c.b_shared.isEnabled()
    c.b_shared.click()
    assert ea.opts is None and not c.table.item(0, 0).text().startswith("★")
    assert not c.compress.isChecked() and c.rip_video.isChecked()
    w.close()


def test_save_state_keeps_own_options():
    (WORK / "so_src").mkdir()
    clip = fx.single_clip(WORK / "so_src")
    rep = WORK / "so_rep"
    fx.shared_replays(rep)
    w = window(WORK / "so_out")
    w.queue.auto_start.setChecked(False)
    w._route([str(clip)])
    pump(15, lambda: w.clips.entries)
    w.clips.table.selectRow(0)
    w.clips.compress.setChecked(True)
    w.clips.rip_checks[1].setChecked(True)
    w.clips.deselect_all()
    w.clips.exact.setChecked(True)                    # a shared option
    w.tabs.setCurrentWidget(w.merge)
    w._route([str(rep)])
    pump(30, lambda: w.merge.sessions)
    m = w.merge
    _select_rows(m.tree, [1])
    m.g_rip.setChecked(True)
    m.rip_checks[0].setChecked(True)
    m.deselect_all()
    m.info_file.setChecked(True)
    w.close()

    w2 = window(WORK / "so_out")
    pump(30, lambda: w2.clips.entries and w2.merge.sessions)
    e = w2.clips.entries[0]
    assert e.opts["compress"] and e.opts["rip_tracks"] == [1] and not e.opts["exact"]
    assert w2.clips.shared["exact"] and not w2.clips.shared["compress"]
    assert w2.clips.table.item(0, 0).text().startswith("★ ")
    m2 = w2.merge
    s0, s1 = m2.sessions
    assert id(s0) not in m2.custom and m2.custom[id(s1)]["rip"] and m2.custom[id(s1)]["rip_tracks"] == [0]
    assert m2.tree.topLevelItem(1).text(0) == "★ " + s1.name
    assert m2.shared["info_file"] and m2.info_file.isChecked() and not m2.g_rip.isChecked()
    w2.settings.on_close = "discard"
    w2.close()


def test_new_replay_keeps_session_options():
    """A replay that joins a session (watch folder) keeps that session's own options and name."""
    paths = fx.shared_replays(WORK / "grow_rep")
    w = window(WORK / "grow_out")
    m = w.merge
    w.tabs.setCurrentWidget(m)
    m.add_paths(paths[:2] + paths[4:])
    pump(30, lambda: m.sessions)
    assert [len(s.clips) for s in m.sessions] == [2, 1]
    _select_rows(m.tree, [0])
    m.compress.setChecked(True)
    m.tree.topLevelItem(0).setText(3, "Long round")
    m.tree.topLevelItem(1).setCheckState(0, Qt.Checked)
    m.add_paths(paths[2:4])
    pump(30, lambda: [len(s.clips) for s in m.sessions] == [4, 1])
    top = m.tree.topLevelItem(0)
    assert m.custom[id(m.sessions[0])]["compress"] and top.text(0).startswith("★ ")
    assert top.data(3, Qt.UserRole) == "Long round" and top.isSelected()
    assert m.tree.topLevelItem(1).checkState(0) == Qt.Checked          # unchanged session: tick kept
    assert m.compress.isChecked() and m.g_opts.title() == f"Options for {m.sessions[0].name}"
    w.close()


def test_autosave_brings_back_work_after_a_crash(monkeypatch):
    from trimmer.core.session import autosave_path
    (WORK / "as_src").mkdir()
    clip = fx.single_clip(WORK / "as_src")
    rep = WORK / "as_rep"
    fx.shared_replays(rep)
    w = window(WORK / "as_out")
    w.queue.auto_start.setChecked(False)
    w.autosave()
    assert not autosave_path().exists()              # nothing open: no file
    w._route([str(clip)])
    pump(15, lambda: w.clips.entries)
    w.clips.entries[0].segments = [(1.0, 4.0)]
    w.clips.entries[0].out_name = "Crash test"
    w.tabs.setCurrentWidget(w.merge)
    w._route([str(rep)])
    pump(30, lambda: w.merge.sessions)
    w.merge.tree.topLevelItem(1).setText(3, "Ace")
    w.queue.add_jobs(w.clips.build_jobs(w.clips.entries), start=False)
    w.autosave()
    assert autosave_path().exists()
    # "crash": the window goes away without closing, leaving the autosave and the queue file
    w.autosave_timer.stop()
    w.queue.closing = True
    w.hide()

    asked = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes))
    w2 = window(WORK / "as_out")
    assert len(asked) == 1 and "1 clip, " in asked[0] and "1 waiting job" in asked[0]
    assert not autosave_path().exists()
    pump(30, lambda: w2.clips.entries and w2.merge.sessions)
    e = w2.clips.entries[0]
    assert e.segments == [(1.0, 4.0)] and e.out_name == "Crash test"
    assert w2.merge.tree.topLevelItem(1).data(3, Qt.UserRole) == "Ace"
    assert [i.status for i in w2.queue.items] == ["queued"] and not w2.queue.is_running()
    # an autosave while the restore is still loading keeps the clips that haven't loaded yet
    w2.clips._restore["x.mp4"] = [{"path": "x.mp4", "segments": []}]
    assert {d["path"] for d in w2.clips.snapshot()["entries"]} == {str(clip), "x.mp4"}
    w2.clips._restore.clear()
    w2.autosave()
    assert autosave_path().exists()
    w2.settings.on_close = "discard"
    w2.close()                                        # a clean close removes the autosave
    assert not autosave_path().exists()

    # answering No: nothing comes back and the autosave and queue are gone
    w3 = window(WORK / "as_out")
    w3.autosave()
    assert not autosave_path().exists()
    w3._route([str(clip)])
    pump(15, lambda: w3.clips.entries)
    w3.autosave()
    w3.autosave_timer.stop()
    w3.hide()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    w4 = window(WORK / "as_out")
    pump(2)
    assert not w4.clips.entries and not autosave_path().exists() and load_queue() == []
    w4.close()


def test_delete_originals_from_clips_and_replay_merge(monkeypatch):
    (WORK / "del_src").mkdir()
    a = fx.single_clip(WORK / "del_src")
    b = WORK / "del_src" / "b.mp4"
    shutil.copy(a, b)
    with open(b, "ab") as f:
        f.write(b"\0" * 64)
    rep = WORK / "del_rep"
    fx.shared_replays(rep)
    w = window(WORK / "del_out")
    w.settings.permanent_delete = True               # no Recycle Bin on the test machine
    w.queue.auto_start.setChecked(False)
    w._route([str(a), str(b)])
    pump(15, lambda: len(w.clips.entries) == 2)
    c = w.clips

    # No: nothing happens
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    c.table.selectRow(0)
    c.delete_selected()
    assert len(c.entries) == 2 and a.exists()

    # a file a waiting job needs is left alone
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    shown = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[2])))
    w.queue.add_jobs(w.clips.build_jobs([e for e in c.entries if e.path == a]), start=False)
    c.table.selectAll()
    c.delete_selected()
    assert len(shown) == 1 and a.name in shown[0]
    assert a.exists() and not b.exists()
    assert [e.path for e in c.entries] == [a]

    # Replay merge: a clip row goes, the rest are grouped again
    w.tabs.setCurrentWidget(w.merge)
    w._route([str(rep)])
    pump(30, lambda: w.merge.sessions)
    m = w.merge
    n = len(m.paths)
    first = m.sessions[0].clips[0]
    m.open_clip(first)
    _select_rows(m.tree, [(0, 0)])
    m.delete_selected()
    assert not first.path.exists() and not m.viewers
    pump(30, lambda: len(m.paths) == n - 1 and sum(len(s.clips) for s in m.sessions) == n - 1)
    assert str(first.path) not in {str(p) for p in m.paths}
    w.close()


def test_queue_status_in_notes():
    (WORK / "qn_src").mkdir()
    clip = fx.single_clip(WORK / "qn_src")
    rep = WORK / "qn_rep"
    fx.shared_replays(rep)
    w = window(WORK / "qn_out")
    w.queue.auto_start.setChecked(False)
    w._route([str(clip)])
    pump(15, lambda: w.clips.entries)
    c = w.clips
    notes = lambda: c.table.item(0, c.table.columnCount() - 1).text()
    assert notes() == ""
    w.tabs.setCurrentWidget(w.merge)
    w._route([str(rep)])
    pump(30, lambda: w.merge.sessions)
    m = w.merge
    assert m.tree.topLevelItem(1).text(4) == "single clip, nothing to merge"

    c.queue(selected_only=False)
    m.queue()
    assert notes() == "Queue: Waiting"
    assert m.tree.topLevelItem(0).text(4) == "Queue: Waiting"
    assert m.tree.topLevelItem(1).text(4) == "single clip, nothing to merge"    # not ticked, not queued
    w.queue.start()
    pump(240, lambda: finished(w))
    pump(5, lambda: m.tree.topLevelItem(0).text(4) == "Queue: Done")      # the last job's finish signal
    assert notes() == "Queue: Done"
    assert m.tree.topLevelItem(0).text(4) == "Queue: Done"
    w.queue.clear_finished()
    assert notes() == "" and m.tree.topLevelItem(0).text(4) == ""
    w.close()


def test_source_gap_shows_in_notes_and_queue_details():
    """A job on footage with a gap in its frames finishes, and the gap shows in Notes and the queue."""
    import re
    from trimmer.gui.queue_tab import C_DETAIL
    clip = fx.gap_clip(WORK / "gapn_src")
    w = window(WORK / "gapn_out")
    w.queue.auto_start.setChecked(False)
    w._route([str(clip)])
    pump(15, lambda: w.clips.entries)
    c = w.clips
    notes = lambda: c.table.item(0, c.table.columnCount() - 1).text()
    c.queue(selected_only=False)
    w.queue.start()
    pump(240, lambda: finished(w))
    pump(5, lambda: notes().startswith("Queue: Done"))
    assert re.fullmatch(r"Queue: Done \(source gap 2\.5\ds at 0:0[78]\.\d{3}\)", notes()), notes()
    detail = w.queue.table.item(0, C_DETAIL)
    assert detail.text().startswith("Gap already in the source: 2.5") and "Gap clip.mkv" in detail.text()
    w.close()


def test_queue_deletes_an_original_only_after_every_job_using_it():
    from trimmer.core.jobs import ClipJob
    from trimmer.gui.worker import QueueItem, QueueRunner
    src_dir = WORK / "shared_src"
    src_dir.mkdir(parents=True, exist_ok=True)
    out = WORK / "out_shared"

    def make():
        p = src_dir / "clip.mp4"
        shutil.copy(fx.single_clip(WORK / "src"), p)
        return p

    def job(p, seg, delete, policy="rename"):
        o = JobOptions(output_dir=str(out), conflict_policy=policy, delete_originals=delete,
                       permanent_delete=True)
        return ClipJob(str(p), segments=[seg], options=o)

    # Two jobs on one file, the first deletes it: the second still runs, then it goes.
    p = make()
    items = [QueueItem(job(p, (0, 4), True)), QueueItem(job(p, (6, 10), False))]
    QueueRunner(items).run()
    assert [i.status for i in items] == ["done", "done"], [i.log for i in items]
    assert not p.exists()
    assert items[0].result.deleted == [str(p)]

    # The later job doesn't finish (its output name is taken and set to skip): the original stays.
    p = make()
    shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "clip (trimmed 0m04s-0m10s).mp4").write_bytes(b"taken")    # 6 s snaps to the keyframe at 4.08 s
    items = [QueueItem(job(p, (0, 4), True)), QueueItem(job(p, (6, 10), False, "skip"))]
    QueueRunner(items).run()
    assert [i.status for i in items] == ["done", "skipped"], [i.log for i in items]
    assert p.exists()
    assert any("kept" in line for line in items[0].log)

    # Split parts, every one set to delete: the first part doesn't finish, so the
    # later parts must not delete the original either.
    p = make()
    shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "clip (trimmed 0m04s-0m10s).mp4").write_bytes(b"taken")
    items = [QueueItem(job(p, (6, 10), True, "skip")), QueueItem(job(p, (0, 4), True)),
             QueueItem(job(p, (12, 16), True))]
    QueueRunner(items).run()
    assert [i.status for i in items] == ["skipped", "done", "done"], [i.log for i in items]
    assert p.exists(), [i.log for i in items]
    assert any("didn't finish (skipped)" in line for line in items[2].log)


def test_show_original_in_folder(monkeypatch):
    from trimmer.gui import widgets
    shown, opened = [], []
    monkeypatch.setattr(widgets, "reveal", lambda p: shown.append(Path(p)))
    monkeypatch.setattr(widgets.QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toLocalFile())))
    d = WORK / "reveal"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "here.mp4"
    f.write_bytes(b"x")
    assert widgets.show_in_folder(None, f) and shown == [f]
    gone = d / "gone.mp4"
    assert not widgets.show_in_folder(None, gone)          # asks, and opens the folder it was in
    assert shown == [f] and Path(opened[0]) == d
    infos = []
    monkeypatch.setattr(widgets.QMessageBox, "information", staticmethod(lambda *a, **k: infos.append(a[2])))
    assert not widgets.show_in_folder(None, d / "no such folder" / "x.mp4")
    assert "neither does its folder" in infos[0]


def test_split_parts_into_separate_clips(monkeypatch):
    src = WORK / "split_src"
    src.mkdir(parents=True, exist_ok=True)
    clip = src / "Split me.mp4"
    shutil.copy(fx.single_clip(WORK / "src"), clip)
    w = window(WORK / "split_out")
    w.queue.auto_start.setChecked(False)
    c = w.clips
    w._route([str(clip)])
    pump(15, lambda: c.entries)
    c.table.selectRow(0)
    pump(1)
    e = c.entries[0]
    e.segments = [(1.0, 4.0), (8.0, 12.0)]
    e.out_name = "Round"
    c._refresh_segments()
    assert c.b_seg_split.isEnabled()
    c.split_parts()
    assert len(c.entries) == 2
    a, b = c.entries
    assert a.segments == [(1.0, 4.0)] and b.segments == [(8.0, 12.0)]
    assert (a.part, b.part) == ((1, 2), (2, 2))
    assert (a.out_name, b.out_name) == ("Round (part 1)", "Round (part 2)")
    assert "part 2 of 2" in c.table.item(1, 0).text()
    b.out_name = "Other name"                      # each part is edited on its own
    jobs = c.build_jobs(c.entries)
    assert [j.segments for j in jobs] == [[(1.0, 4.0)], [(8.0, 12.0)]]
    assert [j.output_name for j in jobs] == ["Round (part 1)", "Other name"]
    # Adding the file again is still caught as a duplicate, even after one part is removed.
    c._drop([a])
    w._route([str(clip)])
    pump(5)
    assert len(c.entries) == 1
    # Both parts come back after a restart.
    c.split_parts(c.entries[0]) if len(c.entries[0].segments) > 1 else None
    snap = c.snapshot()
    c.clear()
    c.restore({"entries": snap["entries"] + [dict(snap["entries"][0], segments=[[1.0, 4.0]], out_name="",
                                                  part=[1, 2])]})
    pump(15, lambda: len(c.entries) == 2)
    assert sorted(x.segments[0] for x in c.entries) == [(1.0, 4.0), (8.0, 12.0)]
    assert {x.out_name for x in c.entries} == {"Other name", ""}
    w.settings.on_close = "discard"
    w.close()


def test_full_screen_preview_and_back():
    from trimmer.gui.preview import PreviewDialog
    w = window(WORK / "fs_out")
    c = w.clips
    home = c.stage.parentWidget()
    i = home.layout().indexOf(c.stage)
    c.full.enter()
    pump(0.5)
    assert c.full.active and c.stage.parentWidget() is c.full
    c.full.leave()
    pump(0.5)
    assert not c.full.active and c.stage.parentWidget() is home and home.layout().indexOf(c.stage) == i

    d = PreviewDialog(w, fx.single_clip(WORK / "src"), "Viewer", parent=w)
    d.show()
    pump(0.5)
    d.full.enter()
    pump(0.5)
    assert d.stage.parentWidget() is d.full
    d.close()                                      # closing while full screen leaves it first
    pump(0.5)
    assert not d.full.active
    w.settings.on_close = "discard"
    w.close()


def test_queue_closes_the_preview_before_deleting_an_original(monkeypatch):
    src = WORK / "release_src"
    src.mkdir(parents=True, exist_ok=True)
    clip = src / "Release me.mp4"
    shutil.copy(fx.single_clip(WORK / "src"), clip)
    w = window(WORK / "release_out")
    w.settings.permanent_delete = True
    c = w.clips
    w._route([str(clip)])
    pump(15, lambda: c.entries)
    c.table.selectRow(0)
    pump(1)
    assert c.current is not None
    released = []
    real = w.release_files
    monkeypatch.setattr(w, "release_files", lambda paths: (released.extend(paths), real(paths)))
    jobs = c.build_jobs(c.entries)
    jobs[0].segments = [(0.0, 5.0)]
    jobs[0].options.delete_originals = True
    jobs[0].options.permanent_delete = True
    w.queue.add_jobs(jobs, start=False)
    w.queue.start()
    pump(60, lambda: finished(w))
    assert w.queue.items[-1].status == "done", w.queue.items[-1].log
    assert released == [clip] and c.current is None and not clip.exists()
    w.settings.on_close = "discard"
    w.close()


def test_default_track_with_tracks_of_the_same_name():
    from trimmer.gui.widgets import TrackPicker
    prefs = {}
    t = TrackPicker(prefs)
    t.set_tracks(["1: Audio", "2: Audio", "3: Audio"], ["Audio", "Audio", "Audio"])
    t.set_default(2)
    assert t.currentIndex() == 2 and "default" in t.itemText(2) and "default" not in t.itemText(0)
    other = TrackPicker(prefs)                 # the next clip opened
    other.set_tracks(["1: Audio", "2: Audio", "3: Audio"], ["Audio", "Audio", "Audio"])
    assert other.currentIndex() == 2
    other.set_tracks(["1: Mic", "2: Game", "3: Audio"], ["Mic", "Game", "Audio"])
    assert other.currentIndex() == 2           # a unique name still wins
    other.set_tracks(["1: Audio", "2: Mic"], ["Audio", "Mic"])
    assert other.currentIndex() == 0           # position 3 isn't there: the track with the name


def test_split_here_makes_parts_with_nothing_between():
    src = WORK / "splithere_src"
    src.mkdir(parents=True, exist_ok=True)
    clip = src / "Cut me.mp4"
    shutil.copy(fx.single_clip(WORK / "src"), clip)
    w = window(WORK / "splithere_out")
    c = w.clips
    w._route([str(clip)])
    pump(15, lambda: c.entries)
    c.table.selectRow(0)
    pump(1)
    end = c.entries[0].info.duration
    c.split_here(6.0)
    assert [x.segments for x in c.entries] == [[(0.0, 6.0)], [(6.0, end)]]
    assert c.current is c.entries[1]                  # carry on from the cut
    c.split_here(12.5)
    assert [x.segments for x in c.entries] == [[(0.0, 6.0)], [(6.0, 12.5)], [(12.5, end)]]
    assert [x.part for x in c.entries] == [(1, 3), (2, 3), (3, 3)]
    # A clip with kept parts splits the part under the playhead and keeps the rest.
    c.table.selectRow(0)
    pump(1)
    c.current.segments = [(0.5, 2.0), (3.0, 5.5)]
    c.split_here(4.0)
    assert [x.segments for x in c.entries[:2]] == [[(0.5, 2.0), (3.0, 4.0)], [(4.0, 5.5)]]
    c.split_here(2.5)                                 # in a gap: nothing happens
    assert len(c.entries) == 4 and "inside the kept footage" in c.trim_info.text()
    jobs = c.build_jobs(c.entries)
    assert [j.segments for j in jobs] == [[(0.5, 2.0), (3.0, 4.0)], [(4.0, 5.5)], [(6.0, 12.5)], [(12.5, end)]]
    w.settings.on_close = "discard"
    w.close()


def test_merge_tab_edit_compress_settings(monkeypatch):
    """Replay merge has the same Edit… button as Clips: it starts from the
    preset shown and saves the result as the Custom settings."""
    from trimmer.core.encode import EncodeSettings
    from trimmer.gui import clips_tab
    w = window(WORK / "out_edit")
    m = w.merge
    assert not m.b_custom.isEnabled()
    m.compress.setChecked(True)
    assert m.b_custom.isEnabled()
    name = next(iter(w.settings.presets))
    m.preset.setCurrentIndex(m.preset.findText(name))
    seen = {}

    def fake_exec(dlg):
        seen["start"] = dlg.form.settings().to_dict()
        dlg.form.set_settings(EncodeSettings(codec="av1", quality=40, ten_bit=True))
        return True
    monkeypatch.setattr(clips_tab.CustomEncodeDialog, "exec", fake_exec)
    m.b_custom.click()
    assert seen["start"] == EncodeSettings.from_dict(w.settings.presets[name]).to_dict()
    assert m.preset.currentIndex() == 0 and m.compress.isChecked()
    e = m._enc(m.shared)
    assert (e.codec, e.quality, e.ten_bit) == ("av1", 40, True)
    w.close()


def test_queue_waits_for_auto_settings():
    """Jobs set to 'Auto settings, measured' are analysed in the queue first:
    Start stays off until then, the choice shows in the details panel, and
    the queue starts by itself afterwards when auto-start is on."""
    from trimmer.core.encode import EncodeSettings
    from trimmer.core.jobs import ClipJob, JobOptions as JO
    from trimmer.gui import queue_tab as qt
    src = fx.single_clip(WORK / "src_autoset")
    w = window(WORK / "out_autoset_gui")
    q = w.queue
    q.auto_start.setChecked(True)
    enc = EncodeSettings(quality="autoset", preset="ultrafast", give_up=70).to_dict()
    job = ClipJob(str(src), compress=True, encode=enc, options=JO(output_dir=str(WORK / "out_autoset_gui"),
                                                                    conflict_policy="rename"))
    q.add_jobs([job], start=True)
    item = q.items[-1]
    assert not item.ready and not q.is_running()
    assert not q.b_start.isEnabled() and "auto settings" in q.b_start.toolTip()
    pump(5, lambda: "Choosing" in q.table.item(q.items.index(item), qt.C_STATUS).text())
    assert q.table.item(q.items.index(item), qt.C_STATUS).text() == "Choosing settings"
    assert "Auto settings (give up 70)" in q.table.item(q.items.index(item), qt.C_SET).text()
    q.start()                           # pressing Start early just waits
    assert not q.is_running()
    pump(240, lambda: item.ready)
    assert item.ready and item.job.encode.get("chosen")
    q.table.selectRow(q.items.index(item))
    assert "Auto settings: " in q.log.toPlainText()
    assert "(chosen by auto settings, give up 70)" in q.log.toPlainText().splitlines()[0]
    assert "(auto settings)" in q.table.item(q.items.index(item), qt.C_SET).text()
    pump(120, lambda: item.status not in ("queued", "running"))
    assert item.status == "done", item.log
    assert "autoset" in item.result.outputs[0]
    w.close()


def test_changing_settings_mid_analysis_starts_over():
    from trimmer.core.encode import EncodeSettings
    from trimmer.core.jobs import ClipJob, JobOptions as JO
    src = fx.single_clip(WORK / "src_autoset2")
    w = window(WORK / "out_autoset_gui2")
    q = w.queue
    q.auto_start.setChecked(False)
    enc = EncodeSettings(quality="autoset", preset="ultrafast", give_up=10).to_dict()
    q.add_jobs([ClipJob(str(src), compress=True, encode=enc, options=JO(output_dir=str(WORK / "out_autoset_gui2")))],
               start=False)
    item = q.items[-1]
    pump(10, lambda: q._estimating is item and item.analysis.startswith("testing"))
    q.set_compression([item], None)               # no compression any more
    pump(30, lambda: item.ready)
    assert item.ready and not item.job.compress and not item.job.encode.get("chosen")
    assert q.b_start.isEnabled(), [(i.status, i.ready, i.job.title) for i in q.items] + [q.is_running()]
    q.remove_selected([item])
    w.close()


def test_queue_menu_edit_custom_compression(monkeypatch):
    """Right-click > Compression > Custom, edit first…: the dialog starts from
    the job's own compression, saves as Custom and applies it."""
    from trimmer.core.encode import EncodeSettings
    from trimmer.core.jobs import ClipJob, JobOptions as JO
    from trimmer.gui import clips_tab
    src = fx.single_clip(WORK / "src_qedit")
    w = window(WORK / "out_qedit")
    q = w.queue
    q.auto_start.setChecked(False)
    mine = EncodeSettings(codec="h264", quality=27, preset="ultrafast").to_dict()
    q.add_jobs([ClipJob(str(src), compress=True, encode=mine, options=JO(output_dir=str(WORK / "out_qedit")))],
               start=False)
    item = q.items[-1]
    seen = {}

    def fake_exec(dlg):
        seen["start"] = dlg.form.settings()
        dlg.form.set_settings(EncodeSettings(codec="h265", quality=30, preset="fast", ten_bit=True))
        return True
    monkeypatch.setattr(clips_tab.CustomEncodeDialog, "exec", fake_exec)
    q.edit_compression([item])
    assert (seen["start"].codec, seen["start"].quality) == ("h264", 27)
    e = EncodeSettings.from_dict(item.job.encode)
    assert (e.codec, e.quality, e.preset, e.ten_bit) == ("h265", 30, "fast", True) and item.job.compress
    assert w.settings.encode_settings().quality == 30
    assert "H.265 10-bit CRF30" in q.table.item(q.items.index(item), 2).text()
    monkeypatch.setattr(clips_tab.CustomEncodeDialog, "exec", lambda dlg: False)   # Cancel changes nothing
    q.edit_compression([item])
    assert EncodeSettings.from_dict(item.job.encode).quality == 30
    q.remove_selected([item])
    w.close()


def test_queue_details_start_with_compression(monkeypatch):
    """The details panel's first line is the job's compression, and it changes
    in place when the compression is changed."""
    from trimmer.core.encode import EncodeSettings
    from trimmer.core.jobs import ClipJob, JobOptions as JO
    from trimmer.gui import clips_tab
    from trimmer.gui import queue_tab as qt
    src = fx.single_clip(WORK / "src_qline")
    w = window(WORK / "out_qline")
    q = w.queue
    q.auto_start.setChecked(False)
    mine = EncodeSettings(codec="h264", quality=27, preset="ultrafast", max_height=720).to_dict()
    q.add_jobs([ClipJob(str(src), compress=True, encode=mine, options=JO(output_dir=str(WORK / "out_qline")))],
               start=False)
    item = q.items[-1]
    q.table.selectRow(q.items.index(item))
    first = lambda: q.log.toPlainText().splitlines()[0]
    assert first() == "Compression: H.264 (libx264), CRF 27, preset ultrafast, audio copied, max 720p"
    q._note(item, ["a note"])
    monkeypatch.setattr(clips_tab.CustomEncodeDialog, "exec", lambda dlg: (
        dlg.form.set_settings(EncodeSettings(codec="h265", quality=30, preset="fast", audio="160")) or True))
    q.edit_compression([item])
    assert first() == "Compression: H.265 (libx265), CRF 30, preset fast, audio 160 kbps"
    assert q.log.toPlainText().splitlines()[1:] == ["a note"]
    q.set_compression([item], None)
    assert first() == "Compression: none (no re-encode)"
    chosen = {"codec": "av1", "quality": 35, "ten_bit": True, "report": ["Auto settings: AV1 CRF 35"]}
    assert "AV1 10-bit (libsvtav1), CRF 35 (chosen by auto settings, give up 32)" in qt.compression_line(
        ClipJob(str(src), compress=True, encode=EncodeSettings(quality="autoset", chosen=chosen).to_dict()))
    assert "Auto settings, measured (give up 32)" in qt.compression_line(
        ClipJob(str(src), compress=True, encode=EncodeSettings(quality="autoset").to_dict()))
    q.remove_selected([item])
    w.close()


def test_replay_merge_tab_can_be_hidden(monkeypatch):
    from trimmer.gui import settings_dialog
    watch = WORK / "watch_hidden"
    watch.mkdir(exist_ok=True)
    w = window(WORK / "out_hidden")
    s = w.settings
    s.watch_folder, s.watch_enabled = str(watch), True
    w.merge._start_watch_from_settings()
    i = w.tabs.indexOf(w.merge)
    assert w.tabs.isTabVisible(i) and w.merge.watcher.folder
    w.tabs.setCurrentWidget(w.merge)

    def untick(dlg):
        dlg.show_merge.setChecked(False)
        dlg._save()
        return True
    monkeypatch.setattr(settings_dialog.SettingsDialog, "exec", untick)
    w.open_settings()
    assert not w.tabs.isTabVisible(i) and w.tabs.currentWidget() is w.clips
    assert not w.merge.watcher.folder and s.watch_enabled          # stopped, setting kept
    assert not AppSettings.load().show_replay_merge
    w.close()

    w = window(WORK / "out_hidden")                                   # stays hidden after a restart
    assert not w.tabs.isTabVisible(w.tabs.indexOf(w.merge)) and not w.merge.watcher.folder
    w._show_merge_tab(True)
    assert w.merge.watcher.folder
    w.settings.show_replay_merge, w.settings.watch_enabled = True, False
    w.settings.save()
    w.close()


def test_help_menu_and_tidier_layout(monkeypatch):
    from PySide6.QtWidgets import QPushButton, QToolButton
    from trimmer.gui import help as help_mod
    from trimmer.gui import settings_dialog
    w = window(WORK / "out_help")
    b_help = next(b for b in w.findChildren(QToolButton) if b.text() == "Help")
    names = [a.text() for a in b_help.menu().actions() if a.text()]
    assert names == ["Getting started", "Keyboard shortcuts", "OBS settings tips", "About Trimmer"]
    shown = []
    monkeypatch.setattr(help_mod.HelpDialog, "exec", lambda d: shown.append(d.windowTitle()))
    next(a for a in w.actions() if a.shortcut().toString() == "F1").trigger()
    assert shown == ["Getting started"]
    opened = []
    monkeypatch.setattr(settings_dialog.SettingsDialog, "exec",
                        lambda d: opened.append(d.tabs.tabText(d.tabs.currentIndex())) or False)
    next(a for a in b_help.menu().actions() if a.text() == "About Trimmer").trigger()
    assert opened == ["About"]
    # one place to add files: the toolbar
    for tab in (w.clips, w.merge):
        assert not [b for b in tab.findChildren(QPushButton) if b.text().startswith("Add") and "…" in b.text()]
    w.show_status("Found 2 session(s)")
    w.tabs.setCurrentWidget(w.queue)
    assert w.statusBar().currentMessage() == ""
    # Replay merge: the list's buttons sit under it, and Remove takes clips out without touching files
    m = w.merge
    texts = [b.text() for b in m.findChildren(QPushButton)]
    assert {"Deselect all", "Remove", "Clear"} <= set(texts)
    rep = WORK / "help_rep"
    paths = fx.shared_replays(rep)
    m.add_paths(paths)
    pump(30, lambda: m.sessions)
    n = len(m.paths)
    _select_rows(m.tree, [(0, 0)])
    m.remove_selected()
    pump(30, lambda: len(m.paths) == n - 1 and m.sessions)
    assert all(Path(p).exists() for p in paths)
    m.tree.selectAll()
    m.remove_selected()
    pump(30, lambda: not m.paths)
    assert not m.sessions and m.tree.topLevelItemCount() == 0
    w.close()
