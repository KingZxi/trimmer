"""Background threads: the job queue runner and small one-off tasks.

All ffmpeg work happens off the GUI thread. The runner talks back only
through Qt signals; when a name clash needs the user's answer it emits a
signal and waits on an Event the GUI sets.
"""

import threading
import traceback
import uuid
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..core import jobs as J
from ..core import savings
from ..core.ffmpeg import Cancelled, CancelToken
from ..core.journal import CompletedRecord, RunLog, save_queue
from ..core.util import format_size, recycle_or_delete, same_file


class QueueItem:
    def __init__(self, job):
        self.id = uuid.uuid4().hex[:10]
        self.job = job
        self.status = "queued"        # queued | running | done | reverted | failed | skipped | cancelled
        self.stage = ""
        self.fraction = None
        self.eta = None
        self.size = None
        self.result = None
        self.log = []
        self.estimate = None          # predicted output bytes (None = unknown / not yet)
        self.in_bytes = None


class QueueRunner(QThread):
    item_started = Signal(str)
    item_progress = Signal(str, str, object, object, object)   # id, stage, fraction, eta, size
    item_log = Signal(str, str)
    item_finished = Signal(str)
    conflict = Signal(str)          # path; GUI answers via answer_conflict()
    release = Signal(list)          # paths about to be deleted; GUI lets go, then calls released()
    paused_changed = Signal(bool)   # True while held (mid-job or between jobs)
    batch_finished = Signal(str, bool)    # summary text, True if the user stopped it

    def __init__(self, items, parent=None, count_kept=None):
        super().__init__(parent)
        self.items = items              # shared list owned by the GUI
        self.count_kept = count_kept or (lambda: False)   # read when each job finishes
        self.cancel_all = False
        self.current_token = None
        self.hold_after = False         # "pause after this job"
        self._go = threading.Event()    # cleared while paused
        self._go.set()
        self._answer = None
        self._answer_event = threading.Event()
        self._released = threading.Event()
        # Originals a finished job would have deleted but a later queued job
        # still reads: [{"path", "item", "ok"}]. Removed once no waiting job
        # needs them, and only if every job that used them saved its outputs.
        self.pending_deletes = []

    # Called from the GUI thread.
    def cancel_current(self):
        if self.current_token:
            self.current_token.cancel()

    def stop(self):
        self.cancel_all = True
        self._go.set()
        self.cancel_current()

    def pause(self):
        """Freeze the running job where it is; nothing new starts."""
        self._go.clear()
        if self.current_token:
            self.current_token.pause()
        self.paused_changed.emit(True)

    def resume(self):
        self.hold_after = False
        self._go.set()
        if self.current_token:
            self.current_token.resume()
        self.paused_changed.emit(False)

    @property
    def paused(self) -> bool:
        return not self._go.is_set()

    def answer_conflict(self, answer: str):
        self._answer = answer
        self._answer_event.set()

    def released(self):
        self._released.set()

    def _release(self, paths):
        """Have the GUI close previews of files about to be deleted (an open
        player keeps a file locked on Windows). Never waits long."""
        self._released.clear()
        self.release.emit([str(p) for p in paths])
        self._released.wait(10)

    def _ask(self, path: Path) -> str:
        self._answer_event.clear()
        self.conflict.emit(str(path))
        self._answer_event.wait()
        return self._answer or "rename"

    # ------------------------------------------------------ deferred deletes
    def _needed_later(self, path, item) -> bool:
        return any(i is not item and i.status in ("queued", "running")
                   and any(same_file(path, s) for s in i.job.sources()) for i in list(self.items))

    def _after_job(self, item):
        """Note a deferred original's new users, then remove what nothing waits for."""
        if item.result is not None and item.result.deferred:
            for p in item.result.deferred:
                self.pending_deletes.append({"path": p, "item": item, "ok": True})
        for d in self.pending_deletes:
            if d["item"] is not item and any(same_file(d["path"], s) for s in item.job.sources()) \
                    and item.status not in ("done", "reverted"):
                d["ok"] = False
                self._note(d["item"], f"Original {Path(d['path']).name} kept: {item.job.title} "
                                      f"didn't finish ({item.status}).")
        self._settle_deletes()

    def _settle_deletes(self):
        for d in list(self.pending_deletes):
            if self._needed_later(d["path"], None):
                continue
            self.pending_deletes.remove(d)
            owner, path = d["item"], Path(d["path"])
            if not d["ok"] or not path.exists():
                continue
            try:
                size = path.stat().st_size
                self._release([path])
                what = recycle_or_delete(path, permanent=owner.job.options.permanent_delete)
            except OSError as e:
                self._note(owner, f"Couldn't remove original {path.name}: {e}")
                continue
            res = owner.result
            savings.record_late_delete(res, size, self.count_kept())
            res.deleted.append(str(path))
            res.deleted_bytes += size
            self._note(owner, f"Original {path.name} {what} (the later jobs that used it are done)")
            self.item_finished.emit(owner.id)

    def _note(self, item, msg):
        item.log.append(msg)
        self.item_log.emit(item.id, msg)

    def run(self):
        record = CompletedRecord()
        runlog = RunLog("per-job settings (see each job)")
        runlog.write("Status: IN PROGRESS -- starting")
        cancelled = False
        while not self.cancel_all:
            while not self._go.wait(0.25):      # paused between jobs
                if self.cancel_all:
                    break
            if self.cancel_all:
                break
            item = next((i for i in self.items if i.status == "queued"), None)
            if item is None:
                break
            item.status = "running"
            self.item_started.emit(item.id)
            runlog.write(f"Status: IN PROGRESS -- currently processing: {item.job.title}")
            token = CancelToken()
            if not self._go.is_set():
                token.pause()
            self.current_token = token

            def log(msg, item=item):
                item.log.append(msg)
                self.item_log.emit(item.id, msg)

            def progress(stage, p, item=item):
                self.item_progress.emit(item.id, stage, p.fraction, p.eta, p.size)

            ctx = J.JobContext(log=log, progress=progress, resolve_conflict=self._ask, cancel=token,
                               still_needed=lambda p, item=item: self._needed_later(p, item))
            ctx.release = self._release
            key = J.job_key(item.job)
            try:
                res = J.run_job(item.job, ctx)
                savings.record(res, self.count_kept())      # before the status flips, so the GUI sees it
                item.result = res
                item.status = res.status
                if res.status in ("done", "reverted"):
                    record.add(key, res.outputs)
                text = {"done": "done", "reverted": "kept original (compressed would be larger)",
                        "skipped": "skipped", "failed": "; ".join(res.messages)[:200]}.get(res.status, res.status)
                if res.in_bytes and res.out_bytes:
                    text += f"   {format_size(res.in_bytes)} -> {format_size(res.out_bytes)}"
                runlog.record(item.job.title, res.status, text, res.in_bytes, res.out_bytes)
            except Cancelled:
                item.status = "cancelled"
                log("Cancelled. Any partial output was removed.")
                runlog.record(item.job.title, "cancelled", "cancelled by user")
            except Exception as e:      # last resort: keep the app alive, report it
                item.status = "failed"
                log(f"Unexpected error: {e}\n{traceback.format_exc()}")
                runlog.record(item.job.title, "failed", f"unexpected error: {e}")
            self.current_token = None
            self.item_finished.emit(item.id)
            self._after_job(item)
            save_queue([J.job_to_dict(i.job) for i in self.items if i.status == "queued"])
            if self.cancel_all:
                cancelled = True
            if self.hold_after and not self.cancel_all and any(i.status == "queued" for i in self.items):
                self.hold_after = False
                self._go.clear()
                self.paused_changed.emit(True)
        self._settle_deletes()
        t = runlog.totals
        saved = t["in_bytes"] - t["out_bytes"]
        summary = (f"{t.get('done', 0)} done, {t.get('reverted', 0)} kept original, "
                   f"{t.get('failed', 0)} failed, {t.get('skipped', 0)} skipped")
        if t["in_bytes"] and t["out_bytes"]:
            summary += f". Space saved: {format_size(saved)} ({saved / t['in_bytes'] * 100:.0f}%)"
        runlog.write(("Status: CANCELLED BY USER" if cancelled or self.cancel_all else "Status: COMPLETED")
                     + f" -- {t['attempted']} job(s) attempted")
        self.batch_finished.emit(summary, cancelled or self.cancel_all)


class Task(QThread):
    """Run a function in the background; emits done(result) or failed(text)."""
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, parent=None):
        super().__init__(parent)
        self.fn, self.args = fn, args

    def run(self):
        try:
            self.done.emit(self.fn(*self.args))
        except Exception as e:
            self.failed.emit(str(e))
