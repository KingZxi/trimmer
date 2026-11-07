"""Queue tab: runs jobs one at a time. Waiting jobs can be renamed, moved,
re-pointed to another folder, given other compression, or reordered;
the running job can be paused and resumed in place."""

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFrame, QHeaderView,
                               QInputDialog, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QSplitter, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import ffmpeg as ff
from ..core import jobs as J
from ..core import savings
from ..core.describe import compression_line, default_name, describe, gap_note
from ..core.encode import EncodeSettings
from ..core.estimate import prepare_job
from ..core.journal import save_queue
from ..core.util import format_hms, format_size, logs_dir, sanitize_filename
from .clips_tab import CustomEncodeDialog
from .theme import set_kind
from .widgets import HintTable, hbox, label, show_in_folder
from .worker import QueueItem, QueueRunner, Task

STATUS_TEXT = {"queued": "Waiting", "running": "Working", "done": "Done", "reverted": "Kept original",
               "failed": "Failed", "skipped": "Skipped", "cancelled": "Cancelled"}
COLS = ["Job", "Saves to", "Settings", "Status", "Progress", "Details", "Size", "Delete original"]
C_NAME, C_DEST, C_SET, C_STATUS, C_BAR, C_DETAIL, C_SIZE, C_DEL = range(len(COLS))
WHEN_DONE = [("Do nothing", "nothing"), ("Open the output folder", "open"), ("Put the PC to sleep", "sleep")]


# ------------------------------------------------------------------ helpers
def sleep_pc():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.powrprof.SetSuspendState(False, False, False)    # sleep, not hibernate
    elif sys.platform == "darwin":
        subprocess.Popen(["pmset", "sleepnow"])
    else:
        subprocess.Popen(["systemctl", "suspend"])


# --------------------------------------------------------------------- tab
class QueueTab(QWidget):
    status = Signal(str)
    running_changed = Signal(bool)
    jobs_changed = Signal()         # jobs added, started, finished or removed

    def __init__(self, main, parent=None):
        super().__init__(parent)
        self.main = main
        self.items = []
        self.runner = None
        self.closing = False        # set on quit so late signals don't overwrite the saved queue
        self.paused = False
        self._estimating = None
        self._est_cancel = None
        self._est_task = None
        self._to_estimate = []
        self._start_when_ready = False   # auto-start once every job's settings are chosen
        self.on_finished = {}       # id(job) -> callback(item), for jobs another tab wants to follow up on
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        ui = self.main.settings.ui
        self.b_start = set_kind(QPushButton("Start"), "primary")
        self.b_start.setMinimumWidth(84)
        self.b_start.clicked.connect(self.start_or_resume)
        self.b_pause = QPushButton("Pause")
        self.b_pause.setToolTip("Freeze the current job exactly where it is. Resume carries on from there.")
        self.b_pause.clicked.connect(self.pause)
        self.b_cancel = QPushButton("Cancel current")
        self.b_cancel.clicked.connect(self.cancel_current)
        self.b_stop = QPushButton("Stop all")
        self.b_stop.clicked.connect(self.stop_all)
        self.pause_after = QCheckBox("Pause after this job")
        self.pause_after.toggled.connect(self._pause_after)
        self.auto_start = QCheckBox("Start jobs automatically")
        self.auto_start.setToolTip("When off, added jobs wait here until you press Start")
        self.auto_start.setChecked(bool(ui.get("auto_start_queue", True)))
        self.auto_start.toggled.connect(lambda on: self._save_ui("auto_start_queue", bool(on)))
        b_up = set_kind(QPushButton("Move up"), "ghost")
        b_up.clicked.connect(lambda: self.move(-1))
        b_down = set_kind(QPushButton("Move down"), "ghost")
        b_down.clicked.connect(lambda: self.move(1))
        self.b_retry = set_kind(QPushButton("Retry"), "ghost")
        self.b_retry.clicked.connect(lambda: self.retry())
        b_rm = set_kind(QPushButton("Remove"), "ghost")
        b_rm.clicked.connect(lambda: self.remove_selected())
        b_clear = set_kind(QPushButton("Clear finished"), "ghost")
        b_clear.clicked.connect(self.clear_finished)
        lay.addWidget(hbox(self.b_start, self.b_pause, self.b_cancel, self.b_stop, self.pause_after,
                           self.auto_start, "stretch", b_up, b_down, self.b_retry, b_rm, b_clear,
                           stretch_end=False))

        split = QSplitter(Qt.Vertical)
        self.table = HintTable(0, len(COLS))
        self.table.set_hint("Nothing queued", "Jobs you add from Clips or Replay merge show up here")
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.setFrameShape(QFrame.NoFrame)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.cellDoubleClicked.connect(self._double_clicked)
        self.table.itemChanged.connect(self._item_changed)
        h = self.table.horizontalHeader()
        h.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        h.setHighlightSections(False)
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setStretchLastSection(False)
        h.setSectionResizeMode(C_SET, QHeaderView.Stretch)
        for c, w in ((C_NAME, 210), (C_DEST, 190), (C_STATUS, 120), (C_BAR, 150), (C_DETAIL, 170),
                     (C_SIZE, 230), (C_DEL, 110)):
            self.table.setColumnWidth(c, w)
        self.table.itemSelectionChanged.connect(self._selection)
        split.addWidget(self.table)
        self._shown, self._shown_line = None, ""     # job in the details panel, its first line
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFrameShape(QFrame.NoFrame)
        self.log.setPlaceholderText("Select a job to see what it did. Right-click jobs for more options; "
                                    "double-click a name or folder to change it.")
        split.addWidget(self.log)
        split.setSizes([340, 170])
        lay.addWidget(split, 1)

        self.sel_info = label("", "muted")
        self.summary = label("", "muted")
        self.savings = label("", "muted")
        self.savings.setToolTip(
            "Space saved counts originals minus outputs for jobs that deleted their originals (or for every "
            "job, if you turned on 'Count space saved even when originals are kept' in Settings). The "
            "'smaller than their sources' figure counts every finished job. Only jobs since the date shown "
            "are counted; it is an estimate. Reset it in Settings.")
        self.when_done = QComboBox()
        for text, key in WHEN_DONE:
            self.when_done.addItem(text, key)
        self.when_done.setCurrentIndex(max(0, self.when_done.findData(ui.get("when_queue_done", "nothing"))))
        self.when_done.currentIndexChanged.connect(
            lambda _: self._save_ui("when_queue_done", self.when_done.currentData()))
        b_out = set_kind(QPushButton("Open output folder"), "ghost")
        b_out.clicked.connect(lambda: self.open_output())
        b_logs = set_kind(QPushButton("Logs"), "ghost")
        b_logs.clicked.connect(lambda: self._open(logs_dir()))
        lay.addWidget(hbox(self.sel_info, self.summary, "stretch", label("When the queue finishes", "muted"), self.when_done,
                           b_out, b_logs, stretch_end=False))
        lay.addWidget(self.savings)
        self.refresh_savings()
        self._buttons()

    def refresh_savings(self):
        self.savings.setText(savings.summary_text())

    def _save_ui(self, key, value):
        self.main.settings.ui[key] = value
        self.main.settings.save()

    def auto_starts(self) -> bool:
        return self.auto_start.isChecked()

    def is_running(self):
        return self.runner is not None and self.runner.isRunning()

    def _buttons(self):
        running = self.is_running()
        waiting = any(i.status == "queued" for i in self.items)
        choosing = self._choosing()
        self.b_start.setText("Resume" if running and self.paused else "Start")
        self.b_start.setEnabled((running and self.paused) or (not running and waiting and not choosing))
        self.b_start.setToolTip(f"Waiting for auto settings to be chosen for {choosing} job(s)"
                                if choosing and not running else "")
        self.b_pause.setEnabled(running and not self.paused)
        self.b_cancel.setEnabled(running)
        self.b_stop.setEnabled(running)
        sel = self._selected()
        self.b_retry.setEnabled(any(i.status in ("failed", "cancelled", "skipped") for i in sel))
        self.jobs_changed.emit()

    def _choosing(self) -> int:
        """Waiting jobs whose auto settings aren't chosen yet."""
        return sum(1 for i in self.items if i.status == "queued" and not i.ready)

    def note_for(self, sources) -> str:
        """The queue status of the latest job made from exactly these files, for the Notes column of
        Clips and Replay merge ("" when none is in the queue)."""
        want = sorted(str(p) for p in sources)
        item = next((i for i in reversed(self.items) if sorted(i.job.sources()) == want), None)
        if item is None:
            return ""
        text = "Waiting (paused)" if item.status == "queued" and self.paused else STATUS_TEXT.get(item.status, item.status)
        gaps = gap_note(item.result, short=True) if item.status in ("done", "reverted") else ""
        return "Queue: " + text + (f" ({gaps})" if gaps else "")

    # ---------------------------------------------------------------- rows
    def add_jobs(self, jobs, start=True):
        for job in jobs:
            item = QueueItem(job)
            self.items.append(item)
            r = self.table.rowCount()
            self.table.insertRow(r)
            self._make_row(r, item)
            self._to_estimate.append(item)
        self._persist()
        self._estimate_next()
        if start and self.auto_start.isChecked() and self._choosing() and not self.is_running():
            self._start_when_ready = True
            self.status.emit(f"Added {len(jobs)} job(s) to the queue. Choosing auto settings first; "
                             f"the queue starts when that's done.")
        elif start and self.auto_start.isChecked():
            self.status.emit(f"Added {len(jobs)} job(s) to the queue.")
            if not self.is_running():
                self.start()
        else:
            self.status.emit(f"Added {len(jobs)} job(s) to the queue. Press Start on the Queue tab "
                             f"when you're ready.")
        self._buttons()

    def _make_row(self, r, item):
        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setTextVisible(False)
        self.table.setCellWidget(r, C_BAR, bar)
        self._row(item)

    def _rebuild(self, keep_ids=None):
        """Redraw every row (after a reorder or removal)."""
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.setRowCount(len(self.items))
        for r, item in enumerate(self.items):
            self._make_row(r, item)
        self.table.blockSignals(False)
        if keep_ids:
            self.table.clearSelection()
            mode = self.table.selectionMode()
            self.table.setSelectionMode(QAbstractItemView.MultiSelection)
            for r, item in enumerate(self.items):
                if item.id in keep_ids:
                    self.table.selectRow(r)
            self.table.setSelectionMode(mode)
        self._buttons()

    def _index(self, item_id):
        return next((i for i, it in enumerate(self.items) if it.id == item_id), -1)

    def _cell(self, r, c, text, tip=None, muted=False):
        it = QTableWidgetItem(text)
        it.setToolTip(tip if tip is not None else text)
        if muted:
            it.setForeground(self.palette().placeholderText())
        self.table.setItem(r, c, it)
        return it

    def _dest(self, item):
        if item.result is not None and item.result.outputs:
            p = Path(item.result.outputs[0])
            return str(p.parent), str(p)
        d = item.job.options.output_dir
        if d:
            return d, d
        first = Path(item.job.sources()[0]).parent
        return f"Next to original ({first.name})", str(first)

    def _row(self, item):
        r = self.items.index(item)
        job = item.job
        was_blocked = self.table.blockSignals(True)
        renamed = bool(job.output_name.strip())
        self._cell(r, C_NAME, job.title, "From: " + ", ".join(Path(p).name for p in job.sources())
                   + ("" if renamed else "\nDouble-click to rename"))
        shown, full = self._dest(item)
        self._cell(r, C_DEST, shown, full + ("\nDouble-click to change" if item.status == "queued" else ""),
                   muted=not job.options.output_dir and item.result is None)
        self._cell(r, C_SET, describe(job))
        self._cell(r, C_STATUS, self._status_text(item))
        self._cell(r, C_DETAIL, *self._detail_text(item))
        self._cell(r, C_SIZE, self._size_text(item))
        self.table.setItem(r, C_DEL, self._delete_cell(item))
        self.table.blockSignals(was_blocked)
        self._show_compression(item)
        self._show_progress(r, item)

    def _status_text(self, item) -> str:
        status = STATUS_TEXT.get(item.status, item.status)
        if item.status == "running":
            return "Paused" if self.paused else (f"{status}: {item.stage}" if item.stage else status)
        if item.status == "queued" and not item.ready:
            return "Choosing settings" if item is self._estimating else "Waiting to choose settings"
        if item.status == "queued" and self.paused and self.is_running():
            return "Waiting (paused)"
        return status

    def _detail_text(self, item):
        """(the Details cell, its tooltip): progress while running, why it failed or was skipped,
        or the gaps a finished job found in its source."""
        res = item.result
        if item.status == "running":
            left = []
            if item.fraction is not None:
                left.append(f"{item.fraction * 100:.0f}%")
            if item.size:
                left.append(format_size(item.size))
            if item.eta and not self.paused:
                left.append(format_hms(item.eta) + " left")
            return "  ·  ".join(left), None
        if item.status == "queued" and item.analysis:
            return item.analysis, None
        if res is not None and res.messages and item.status in ("failed", "skipped"):
            return res.messages[0].splitlines()[0][:160], None
        if res is not None and item.status in ("done", "reverted") and gap_note(res):
            return gap_note(res)[:160], "\n".join(res.warnings) if res.warnings else None
        return "", None

    def _delete_cell(self, item) -> QTableWidgetItem:
        d = QTableWidgetItem("")
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if item.status == "queued":
            flags |= Qt.ItemIsUserCheckable
        d.setFlags(flags)
        d.setCheckState(Qt.Checked if item.job.options.delete_originals else Qt.Unchecked)
        if item.result is not None and item.result.deleted:
            d.setText(f"{len(item.result.deleted)} removed")
        d.setToolTip("Delete the original(s) once the output is saved and checked"
                     + (" (" + ("permanently" if self.main.settings.permanent_delete else "to the Recycle Bin")
                        + ")"))
        return d

    def _show_progress(self, r: int, item):
        bar = self.table.cellWidget(r, C_BAR)
        if not bar:
            return
        if item.status == "running" and item.fraction is None:
            bar.setRange(0, 0 if not self.paused else 1000)
        else:
            bar.setRange(0, 1000)
            bar.setValue(int((item.fraction or 0) * 1000) if item.status == "running"
                         else 1000 if item.status in ("done", "reverted") else 0)

    def _size_text(self, item):
        rr = item.result
        if rr is not None and item.status in ("done", "reverted"):
            if item.status == "reverted":
                return "Kept original"
            if rr.in_bytes and rr.out_bytes:
                saved = (rr.in_bytes - rr.out_bytes) / rr.in_bytes * 100
                sign = "−" if saved >= 0 else "+"
                return f"{format_size(rr.in_bytes)} → {format_size(rr.out_bytes)}  ({sign}{abs(saved):.0f}%)"
            return format_size(rr.out_bytes) if rr.out_bytes else ""
        if item.estimate:
            base = f"{format_size(item.in_bytes)} → " if item.in_bytes else ""
            return f"{base}≈ {format_size(item.estimate)}"
        if item.in_bytes:
            return format_size(item.in_bytes)
        return "estimating…" if item in self._to_estimate or item is self._estimating else ""

    def _persist(self):
        if self.closing:
            return
        save_queue([J.job_to_dict(i.job) for i in self.items if i.status in ("queued", "running")])

    # ----------------------------------------------------------- estimates
    def _estimate_next(self):
        if self._estimating is not None or not self._to_estimate:
            return
        item = self._to_estimate.pop(0)
        if item not in self.items or item.status != "queued":
            self._estimate_next()
            return
        self._estimating = item
        self._est_cancel = token = ff.CancelToken()
        job = item.job
        asked = dict(job.encode or {})       # the settings being analysed
        t = Task(lambda: prepare_job(replace(job, encode=asked), token, lambda d, n: t.step.emit(d, n)),
                 parent=self)
        if not item.ready:
            item.analysis = "starting…"
            self._row(item)

        def step(d, n, item=item):
            item.analysis = f"testing samples {d}/{n}"
            if item in self.items:
                self._row(item)

        def done(res, item=item, job=job):
            if job.encode != asked:
                self._estimate_done(item)       # changed meanwhile: it is queued again
                return
            item.estimate, item.in_bytes, chosen = res
            if chosen is not None:
                job.encode = {**job.encode, "chosen": chosen}
                self._note(item, chosen["report"])
                self._persist()
            self._estimate_done(item)

        def failed(msg, item=item, job=job):
            if token.cancelled or job.encode != asked:
                self._estimate_done(item)
                return
            if J.needs_analysis(job):
                self._note(item, [f"Couldn't choose auto settings: {msg}",
                                  "They'll be chosen again when the job runs."])
            self._estimate_done(item, give_up=True)

        t.step.connect(step)
        t.done.connect(done)
        t.failed.connect(failed)
        t.start()
        self._est_task = t

    def _note(self, item, lines):
        item.log.extend(lines)
        sel = self._selected()
        if len(sel) == 1 and sel[0] is item:
            self.log.appendPlainText("\n".join(lines))

    def _estimate_done(self, item, give_up=False):
        """`give_up`: the analysis failed; the job itself tries again when it runs."""
        self._estimating = None
        self._est_cancel = None
        item.analysis = ""
        item.ready = give_up or not J.needs_analysis(item.job)
        if item in self.items:
            self._row(item)
            self._selection_size()
        self._buttons()
        if self._start_when_ready and not self._choosing():
            self._start_when_ready = False
            if self.auto_start.isChecked() and not self.is_running() and not self.closing:
                self.start()
        self._estimate_next()

    def stop_estimates(self, wait_ms=0):
        """Stop the running estimate or analysis (on quit)."""
        if self._est_cancel is not None:
            self._est_cancel.cancel()
        if wait_ms and self._est_task is not None and self._est_task.isRunning():
            self._est_task.wait(wait_ms)

    def _reestimate(self, items):
        for it in items:
            it.estimate = None
            it.ready = not J.needs_analysis(it.job)
            if it is self._estimating and self._est_cancel is not None:
                self._est_cancel.cancel()       # its settings changed under it
            if it not in self._to_estimate:
                self._to_estimate.append(it)
            if it in self.items:
                self._row(it)
        self._buttons()
        self._estimate_next()

    # ----------------------------------------------------------------- run
    def start_or_resume(self):
        if self.is_running() and self.paused:
            self.runner.resume()
        else:
            self.start()

    def start(self):
        if self.is_running() or not any(i.status == "queued" for i in self.items):
            return
        if self._choosing():
            self._start_when_ready = True
            self.status.emit("Choosing auto settings first; the queue starts when that's done.")
            self._buttons()
            return
        self.paused = False
        self.runner = QueueRunner(self.items, self, lambda: self.main.settings.count_kept_savings)
        self.runner.hold_after = self.pause_after.isChecked()
        self.runner.item_started.connect(self._started)
        self.runner.item_progress.connect(self._progress)
        self.runner.item_log.connect(self._log_line)
        self.runner.item_finished.connect(self._finished)
        self.runner.conflict.connect(self._conflict)
        self.runner.release.connect(self._release)
        self.runner.paused_changed.connect(self._paused_changed)
        self.runner.batch_finished.connect(self._batch_done)
        self.runner.start()
        self.running_changed.emit(True)
        self._buttons()

    def pause(self):
        if self.is_running():
            self.runner.pause()

    def _pause_after(self, on):
        if self.runner is not None:
            self.runner.hold_after = on

    def _paused_changed(self, paused):
        self.paused = paused
        if paused:
            self.pause_after.setChecked(False)
            self.status.emit("Queue paused. Press Resume to carry on.")
        for it in self.items:
            if it.status in ("running", "queued"):
                self._row(it)
        self.running_changed.emit(self.is_running())
        self._buttons()

    def cancel_current(self):
        if self.runner:
            self.runner.cancel_current()

    def stop_all(self):
        if self.runner:
            self.runner.stop()

    def _started(self, item_id):
        i = self._index(item_id)
        if i >= 0:
            item = self.items[i]
            item.stage = "starting"
            self._row(item)
            if not self._selected():
                self.table.selectRow(i)
        self._buttons()

    def _progress(self, item_id, stage, fraction, eta, size):
        i = self._index(item_id)
        if i < 0:
            return
        item = self.items[i]
        item.stage, item.fraction, item.eta, item.size = stage, fraction, eta, size
        self._row(item)

    def _log_line(self, item_id, msg):
        i = self._index(item_id)
        sel = self._selected()
        if i >= 0 and len(sel) == 1 and sel[0] is self.items[i]:
            self.log.appendPlainText(msg)

    def _follow_up(self, item):
        """Hand a finished (or removed) job to the tab that asked to hear about it."""
        fn = self.on_finished.pop(id(item.job), None)
        if fn is not None and not self.closing:
            fn(item)

    def _finished(self, item_id):
        i = self._index(item_id)
        if i >= 0:
            self._row(self.items[i])
            self._selection_size()
            self._follow_up(self.items[i])
        self.refresh_savings()
        self._persist()
        self._buttons()

    def _batch_done(self, summary, stopped):
        if self.closing:
            return
        self.paused = False
        self.summary.setText("Last run: " + summary)
        self.status.emit("Queue finished. " + summary)
        self.running_changed.emit(False)
        self._buttons()
        self.main.notify_done(summary)
        if stopped:
            return
        action = self.when_done.currentData()
        if action == "open":
            self.open_output(last=True)
        elif action == "sleep":
            self._sleep_countdown()

    def _sleep_countdown(self, seconds=30):
        box = QMessageBox(QMessageBox.Information, "Queue finished",
                          f"The PC will go to sleep in {seconds} seconds.", QMessageBox.Cancel, self)
        left = [seconds]
        timer = QTimer(box)

        def tick():
            left[0] -= 1
            if left[0] <= 0:
                timer.stop()
                box.done(0)
                sleep_pc()
            else:
                box.setText(f"The PC will go to sleep in {left[0]} seconds.")
        timer.timeout.connect(tick)
        timer.start(1000)
        box.open()

    def _release(self, paths):
        try:
            self.main.release_files([Path(p) for p in paths])
        finally:
            self.runner.released()

    def _conflict(self, path):
        box = QMessageBox(self)
        box.setWindowTitle("File already exists")
        box.setText(f"{Path(path).name} already exists in the output folder.")
        box.setInformativeText("What should happen to the new file?")
        b_rename = box.addButton("Keep both (rename new)", QMessageBox.AcceptRole)
        b_over = box.addButton("Overwrite", QMessageBox.DestructiveRole)
        b_skip = box.addButton("Skip", QMessageBox.RejectRole)
        b_cancel = box.addButton("Cancel job", QMessageBox.RejectRole)
        all_box = QCheckBox("Do this for every clash in this run")
        box.setCheckBox(all_box)
        box.exec()
        clicked = box.clickedButton()
        ans = {b_rename: "rename", b_over: "overwrite", b_skip: "skip", b_cancel: "cancel"}.get(clicked, "rename")
        if all_box.isChecked() and ans != "cancel":
            ans += "_all"
        self.runner.answer_conflict(ans)

    # ------------------------------------------------------------ editing
    def _selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.items[r] for r in rows if 0 <= r < len(self.items)]

    def _selection(self):
        sel = self._selected()
        self.log.clear()
        self._shown = None
        if len(sel) == 1:
            self._shown, self._shown_line = sel[0], compression_line(sel[0].job)
            self.log.setPlainText("\n".join([self._shown_line] + sel[0].log))
        self._selection_size(sel)
        self._buttons()

    def _show_compression(self, item):
        """Keep the details panel's first line in step with the job's compression
        (changed from the menu, or chosen by auto settings)."""
        if item is not self._shown:
            return
        line = compression_line(item.job)
        if line == self._shown_line:
            return
        self._shown_line = line
        bar = self.log.verticalScrollBar()
        at = bar.value()
        cur = QTextCursor(self.log.document().firstBlock())
        cur.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        cur.insertText(line)
        bar.setValue(at)

    def _selection_size(self, sel=None):
        """Bottom-left: total size of the selected jobs' originals and outputs."""
        sel = self._selected() if sel is None else sel
        if not sel:
            self.sel_info.setText("")
            return
        src = out = 0
        src_known = out_known = True
        exact = True        # False once any output figure is a prediction
        for it in sel:
            rr = it.result
            if rr is not None and rr.in_bytes:
                src += rr.in_bytes
            elif it.in_bytes:
                src += it.in_bytes
            else:
                src_known = False
            if rr is not None and it.status in ("done", "reverted") and rr.out_bytes:
                out += rr.out_bytes
            elif it.estimate:
                out += it.estimate
                exact = False
            else:
                out_known = False
        n = len(sel)
        text = f"{n} selected  ·  {format_size(src)}{'' if src_known else '+'}"
        if out and out_known:
            text += f"  →  {'' if exact else '≈ '}{format_size(out)}"
        self.sel_info.setText(text + "     ")

    def _waiting(self, items):
        return [i for i in items if i.status == "queued"]

    def _edited(self, items):
        for it in items:
            self._row(it)
        self._persist()

    def rename(self, item):
        if item.status != "queued":
            return
        job = item.job
        start = job.output_name or (default_name(job, self.main.settings)
                                    if self.main.settings.prefill_names else "")
        text, ok = QInputDialog.getText(self, "Rename output", "Save as:", text=start)
        if not ok:
            return
        text = text.strip()
        job.output_name = sanitize_filename(text) if text and text != default_name(job, self.main.settings) else ""
        self._edited([item])

    def change_folder(self, items=None):
        items = self._waiting(items if items is not None else self._selected())
        if not items:
            return
        start = items[0].job.options.output_dir or str(Path(items[0].job.sources()[0]).parent)
        d = QFileDialog.getExistingDirectory(self, "Save these jobs to…", start)
        if d:
            for it in items:
                it.job.options.output_dir = d
            self._edited(items)

    def folder_next_to_original(self, items):
        for it in self._waiting(items):
            it.job.options.output_dir = ""
        self._edited(self._waiting(items))

    def set_compression(self, items, preset):
        """preset: None = no compression, "" = Custom (Settings), else a preset name."""
        s = self.main.settings
        items = self._waiting(items)
        for it in items:
            if preset is None:
                it.job.compress = False
            else:
                it.job.compress = True
                it.job.encode = (s.presets[preset] if preset in s.presets else s.encode_settings().to_dict())
                if isinstance(it.job, J.ClipJob) and not it.job.make_video:
                    it.job.compress = False
        self._edited(items)
        self._reestimate(items)

    def edit_compression(self, items):
        """Edit the Custom settings (starting from the first job's own
        compression, if it has any), then give them to these jobs."""
        items = self._waiting(items)
        if not items:
            return
        first = items[0].job
        start = (replace(EncodeSettings.from_dict(first.encode), chosen={}, method="")
                 if first.compress and first.encode else self.main.settings.encode_settings())
        dlg = CustomEncodeDialog(start, self.main.encoders, self)
        if not dlg.exec():
            return
        enc = dlg.form.settings()
        problems = enc.problems()
        if problems:
            QMessageBox.warning(self, "Compression settings", "\n".join(problems))
            return
        self.main.settings.encode = enc.to_dict()
        self.main.settings.save()
        self.set_compression(items, "")

    def set_delete(self, items, on):
        items = self._waiting(items)
        if on and items and not self._confirm_delete(len(items)):
            self._edited(items)
            return
        for it in items:
            it.job.options.delete_originals = on
        self._edited(items)

    def _confirm_delete(self, n):
        where = "permanently deleted" if self.main.settings.permanent_delete else "moved to the Recycle Bin"
        return QMessageBox.question(self, "Delete originals?",
                                    f"The original file(s) for {n} job(s) will be {where} after their "
                                    f"outputs are saved and checked. Continue?") == QMessageBox.Yes

    def _item_changed(self, cell):
        if cell.column() != C_DEL:
            return
        r = cell.row()
        if 0 <= r < len(self.items):
            item = self.items[r]
            on = cell.checkState() == Qt.Checked
            if on != item.job.options.delete_originals:
                self.set_delete([item], on)

    def _double_clicked(self, r, c):
        if not 0 <= r < len(self.items):
            return
        item = self.items[r]
        if c == C_NAME:
            if item.status == "queued":
                self.rename(item)
            elif item.result is not None and item.result.outputs:
                self._open_file(Path(item.result.outputs[0]))
        elif c == C_DEST and item.status == "queued":
            self.change_folder([item])

    def move(self, step, to_end=False):
        sel = self._selected()
        if not sel:
            return
        ids = {i.id for i in sel}
        if to_end:
            rest = [i for i in self.items if i.id not in ids]
            self.items[:] = (sel + rest) if step < 0 else (rest + sel)
        else:
            n = len(self.items)
            order = range(1, n) if step < 0 else range(n - 2, -1, -1)
            for i in order:
                j = i + step
                if self.items[i].id in ids and self.items[j].id not in ids:
                    self.items[i], self.items[j] = self.items[j], self.items[i]
        self._rebuild(ids)
        self._persist()

    def retry(self, items=None):
        items = [i for i in (items if items is not None else self._selected())
                 if i.status in ("failed", "cancelled", "skipped")]
        for it in items:
            it.status, it.result, it.stage, it.fraction, it.eta, it.size = "queued", None, "", None, None, None
            it.log.append("— retrying —")
            self._row(it)
        if items:
            self._persist()
            self._reestimate(items)
            self.running_changed.emit(self.is_running())
            if self.auto_start.isChecked() and not self.is_running():
                self.start()
        self._buttons()

    def remove_selected(self, items=None):
        items = [i for i in (items if items is not None else self._selected()) if i.status != "running"]
        if not items:
            return
        for it in items:
            self.items.remove(it)
            if it in self._to_estimate:
                self._to_estimate.remove(it)
            if it is self._estimating and self._est_cancel is not None:
                self._est_cancel.cancel()
        self._rebuild()
        self._persist()
        self.running_changed.emit(self.is_running())
        for it in items:
            self._follow_up(it)

    def clear_finished(self):
        self.items[:] = [i for i in self.items if i.status in ("queued", "running")]
        self._rebuild()
        self.log.clear()
        self._shown = None

    # --------------------------------------------------------- right-click
    def _menu(self, pos):
        sel = self._selected()
        if not sel:
            return
        m = QMenu(self)
        waiting = self._waiting(sel)
        one = sel[0] if len(sel) == 1 else None
        if one is not None and one.status == "queued":
            m.addAction("Rename…", lambda: self.rename(one))
        if waiting:
            m.addAction("Change output folder…", lambda: self.change_folder(waiting))
            m.addAction("Save next to the original", lambda: self.folder_next_to_original(waiting))
            comp = m.addMenu("Compression")
            comp.addAction("No compression", lambda: self.set_compression(waiting, None))
            comp.addAction("Custom (Settings)", lambda: self.set_compression(waiting, ""))
            comp.addAction("Custom, edit first…", lambda: self.edit_compression(waiting))
            comp.addSeparator()
            for name in self.main.settings.presets:
                comp.addAction(name, lambda n=name: self.set_compression(waiting, n))
            dele = m.addMenu("Delete original")
            dele.addAction("Yes, after the output is checked", lambda: self.set_delete(waiting, True))
            dele.addAction("No, keep it", lambda: self.set_delete(waiting, False))
            m.addSeparator()
            m.addAction("Move to top", lambda: self.move(-1, to_end=True))
            m.addAction("Move up", lambda: self.move(-1))
            m.addAction("Move down", lambda: self.move(1))
            m.addAction("Move to bottom", lambda: self.move(1, to_end=True))
        if any(i.status in ("failed", "cancelled", "skipped") for i in sel):
            m.addSeparator()
            m.addAction("Retry", lambda: self.retry(sel))
        outs = [Path(p) for i in sel if i.result is not None for p in i.result.outputs]
        m.addSeparator()
        if outs:
            m.addAction("Open output", lambda: self._open_file(outs[0]))
            m.addAction("Show output in folder", lambda: show_in_folder(self, outs[0]))
            m.addAction("Copy output path" + ("s" if len(outs) > 1 else ""),
                        lambda: QGuiApplication.clipboard().setText("\n".join(str(p) for p in outs)))
        if one is not None:
            srcs = [Path(p) for p in one.job.sources()]
            src = next((p for p in srcs if p.exists()), srcs[0])
            if src.exists():
                m.addAction("Open original", lambda: self._open_file(src))
            m.addAction("Show original in folder" if len(srcs) == 1 else "Show originals in folder",
                        lambda: show_in_folder(self, src))
        if any(i.status != "running" for i in sel):
            m.addSeparator()
            m.addAction("Remove from queue", lambda: self.remove_selected(sel))
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------- opening
    def _open(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _open_file(self, path: Path):
        if path.exists():
            if sys.platform == "win32":
                os.startfile(str(path))
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_output(self, last=False):
        pool = self.items if last else (self._selected() or self.items)
        done = [i for i in pool if i.result is not None and i.result.outputs]
        if done:
            out = Path((done[-1] if last else done[0]).result.outputs[0])
            self._open(out.parent)
        elif self.main.output_dir():
            self._open(Path(self.main.output_dir()))
