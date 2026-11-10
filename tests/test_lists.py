"""The lists and controls testers asked about: sorting by a column (Clips, Replay merge, Queue with its
warning), the hand cursor on things you can press, column dividers, and the Auto 3 target slider."""

from pathlib import Path

# First: this sets up the offscreen app and the tests' own data folder.
from tests.gui_helpers import WORK, app, gameplay_clip, pump, replays, window  # noqa: F401
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QPushButton

from trimmer.core.encode import EncodeSettings
from trimmer.core.ffmpeg import AudioTrack, MediaInfo
from trimmer.core.jobs import ClipJob, JobOptions
from trimmer.gui import queue_tab
from trimmer.gui.clips_tab import ClipEntry
from trimmer.gui.theme import stylesheet
from trimmer.gui.widgets import EncodeForm, auto3_words, sort_text
from trimmer.gui import theme


def fake_clip(name, seconds, size, tracks=1):
    info = MediaInfo(path=Path(name), duration=seconds, size=size, has_video=True, video_codec="h264",
                     width=1920, height=1080, fps=60, audio=[AudioTrack(i) for i in range(tracks)])
    return ClipEntry(Path(WORK / name), info)


def names(tab):
    return [tab.table.item(r, 0).text() for r in range(tab.table.rowCount())]


def test_sort_text_counts_numbers():
    assert sorted(["Clip 10", "clip 2", "Clip 1"], key=sort_text) == ["Clip 1", "clip 2", "Clip 10"]


def test_clips_sort_by_column():
    w = window(WORK / "out_sort")
    tab = w.clips
    for e in (fake_clip("Clip 10.mp4", 30, 5_000), fake_clip("Clip 2.mp4", 10, 9_000, tracks=2),
              fake_clip("Clip 1.mp4", 20, 1_000)):
        tab.entries.append(e)
        tab._add_row(e)
    tab.table.selectRow(1)                                    # Clip 2
    hdr = tab.table.horizontalHeader()
    hdr.sectionClicked.emit(0)
    assert [e.path.name for e in tab.entries] == ["Clip 1.mp4", "Clip 2.mp4", "Clip 10.mp4"]
    assert names(tab) == ["Clip 1.mp4", "Clip 2.mp4", "Clip 10.mp4"]       # rows follow the clips
    assert tab.selected_entries()[0].path.name == "Clip 2.mp4"              # selection follows too
    assert hdr.isSortIndicatorShown() and hdr.sortIndicatorOrder() == Qt.AscendingOrder
    hdr.sectionClicked.emit(0)                                              # again: reversed
    assert names(tab) == ["Clip 10.mp4", "Clip 2.mp4", "Clip 1.mp4"]
    assert hdr.sortIndicatorOrder() == Qt.DescendingOrder
    hdr.sectionClicked.emit(1)                                              # Length
    assert [e.info.duration for e in tab.entries] == [10, 20, 30]
    hdr.sectionClicked.emit(2)                                              # Size
    assert [e.info.size for e in tab.entries] == [1_000, 5_000, 9_000]
    hdr.sectionClicked.emit(4)                                              # Audio tracks
    assert tab.entries[-1].path.name == "Clip 2.mp4"
    tab.remove_selected()                                                   # still the right clip
    assert [e.path.name for e in tab.entries] == ["Clip 1.mp4", "Clip 10.mp4"]
    e = fake_clip("Clip 0.mp4", 1, 1)
    tab.entries.append(e)
    tab._add_row(e)
    assert not hdr.isSortIndicatorShown()                                   # added at the end: unsorted
    tab.clear()
    w.close()


def test_merge_sort_by_column():
    w = window(WORK / "out_sort_merge")
    w.tabs.setCurrentWidget(w.merge)
    w._route([str(replays())])
    m = w.merge
    pump(30, lambda: m.tree.topLevelItemCount() == 2)
    assert m.tree.topLevelItemCount() == 2
    before = [s.start for s in m.sessions]
    count = lambda: [len(s.clips) for s in m.sessions]  # noqa: E731
    first_name = m.tree.topLevelItem(0).text(0)
    m.tree.topLevelItem(0).setCheckState(0, Qt.Unchecked)              # a tick that must follow its row
    hdr = m.tree.header()
    hdr.sectionClicked.emit(1)                                         # Clips: 1 then 4
    assert count() == [1, 4]
    hdr.sectionClicked.emit(1)
    assert count() == [4, 1]
    hdr.sectionClicked.emit(0)
    hdr.sectionClicked.emit(0)                                         # newest first
    assert [s.start for s in m.sessions] == sorted(before, reverse=True)
    rows = {m.tree.topLevelItem(i).text(0): (m.sessions[i], m.tree.topLevelItem(i)) for i in range(2)}
    assert rows[first_name][1].checkState(0) == Qt.Unchecked
    assert all(len(s.clips) == it.childCount() for s, it in rows.values())   # rows still match sessions
    m.clear()
    w.close()


def test_queue_sort_warns_and_only_moves_waiting_jobs(monkeypatch):
    clip = str(gameplay_clip())
    w = window(WORK / "out_sort_queue")
    q = w.queue
    w.settings.warn_queue_sort = True
    asked = []

    def box_exec(self):
        asked.append(self.text())
        self.checkBox().setChecked(True)                      # Don't ask again
        b = next(b for b in self.buttons() if b.text() == "Sort")
        b.click()
        return 0

    monkeypatch.setattr(queue_tab.QMessageBox, "exec", box_exec)
    q.add_jobs([ClipJob(clip, segments=[(0, 2)], output_name=n, options=JobOptions(output_dir=str(WORK)))
                for n in ("C", "A", "D", "B")], start=False)
    q.items[0].status = "done"                                # finished jobs keep their place
    titles = lambda: [i.job.title for i in q.items]  # noqa: E731
    hdr = q.table.horizontalHeader()
    hdr.sectionClicked.emit(queue_tab.C_NAME)
    assert asked and "order the jobs run in" in asked[0]
    assert titles() == ["C", "A", "B", "D"]
    assert [q.table.item(r, queue_tab.C_NAME).text() for r in range(4)] == titles()
    assert not w.settings.warn_queue_sort                      # "Don't ask again" stuck
    hdr.sectionClicked.emit(queue_tab.C_NAME)
    assert titles() == ["C", "D", "B", "A"] and len(asked) == 1
    hdr.sectionClicked.emit(queue_tab.C_STATUS)               # not a sorting column
    assert titles() == ["C", "D", "B", "A"]
    q.table.selectRow(3)
    q.move(-1)                                                 # moved by hand: no longer sorted
    assert not hdr.isSortIndicatorShown()
    # Cancel leaves the order alone.
    w.settings.warn_queue_sort = True

    def cancel(self):
        next(b for b in self.buttons() if b.text() != "Sort").click()
        return 0

    monkeypatch.setattr(queue_tab.QMessageBox, "exec", cancel)
    before = titles()
    hdr.sectionClicked.emit(queue_tab.C_NAME)
    assert titles() == before and not hdr.isSortIndicatorShown()
    w.close()


def test_hand_cursor_on_pressable_things():
    theme.apply_theme(app, "dark")              # as Trimmer starts: the theme (and its cursor hook) first
    w = window(WORK / "out_cursor")
    pump(0.2)
    buttons = [b for b in w.findChildren(QPushButton) if b.isVisible()]
    assert buttons
    for b in buttons:
        assert b.cursor().shape() == (Qt.PointingHandCursor if b.isEnabled() else Qt.ArrowCursor), b.text()
    b = buttons[0]
    b.setEnabled(False)
    assert b.cursor().shape() == Qt.ArrowCursor                # disabled: the plain arrow
    b.setEnabled(True)
    assert b.cursor().shape() == Qt.PointingHandCursor
    for c in w.findChildren(QCheckBox) + w.findChildren(QComboBox):
        if c.isVisible() and c.isEnabled():
            assert c.cursor().shape() == Qt.PointingHandCursor
    assert w.tabs.tabBar().cursor().shape() == Qt.PointingHandCursor
    w.close()


def test_column_dividers_in_both_themes():
    for t in (theme.DARK, theme.LIGHT):
        css = stylesheet(t)
        assert f"border-right: 1px solid {t['border']}" in css.split("QHeaderView::section {")[1].split("}")[0]
        assert "QTableView::item { padding: 0 8px; border: none; border-right: 1px solid" in css


def test_auto3_slider_goes_below_90():
    f = EncodeForm()
    f.set_settings(EncodeSettings(codec="h265", quality="auto3", auto3_target=85))
    assert f.auto3_target.minimum() == 80 and f.auto3_target.maximum() == 99
    assert f.settings().auto3_target == 85.0
    assert f.auto3_text.text() == "85: smaller, visible loss"
    f.auto3_target.setValue(93)
    assert f.auto3_text.text() == "93: balanced (default)" and f.settings().auto3_target == 93.0
    assert f.auto3_marks.marks == [95.0, 93.0, 90.0]
    f.set_settings(EncodeSettings(codec="h265", quality="auto3", auto3_target=93.4))    # hand-edited file
    assert f.auto3_target.value() == 93
    assert auto3_words(80) == "smallest, clearly visible loss" and auto3_words(97).startswith("even closer")
    f.resize(600, 400)
    f.show()
    pump(0.05)
    xs = [f.auto3_marks.mark_x(v) for v in (90, 93, 95)]
    assert xs == sorted(xs) and xs[0] > 0                          # marks in order along the slider
    f.close()
