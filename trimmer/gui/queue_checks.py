"""Warnings shown before jobs join the queue: "already queued or done" and
"looks already processed". Both tabs call check_new_jobs() just before
emitting queue_jobs and queue whatever it returns."""

import json
import os
from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QVBoxLayout)

from ..core import jobs as J
from ..core.tag import already_done
from .theme import set_kind

# Jobs that ended without producing anything may be queued again (that's a retry).
_NOT_DONE = ("failed", "cancelled", "skipped")


class ChoiceDialog(QDialog):
    """A list of problems and three ways out. `choice` is "skip", "anyway" or "cancel"."""

    def __init__(self, parent, title, intro, lines, skip_text, anyway_text):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        self.choice = "cancel"
        lay = QVBoxLayout(self)
        head = QLabel(intro)
        head.setWordWrap(True)
        lay.addWidget(head)
        box = QPlainTextEdit("\n".join(lines))
        box.setReadOnly(True)
        box.setMinimumHeight(min(260, 40 + 22 * len(lines)))
        lay.addWidget(box)
        self.dont_ask = QCheckBox("Don't ask again (you can turn this back on in Settings)")
        lay.addWidget(self.dont_ask)
        row = QHBoxLayout()
        row.addStretch(1)
        for text, choice, kind in ((skip_text, "skip", "primary"), (anyway_text, "anyway", None),
                                   ("Cancel", "cancel", "ghost")):
            b = QPushButton(text)
            if kind:
                set_kind(b, kind)
            b.clicked.connect(lambda _=False, c=choice: self._pick(c))
            if choice == "skip":
                b.setDefault(True)
            row.addWidget(b)
        lay.addLayout(row)

    def _pick(self, choice):
        self.choice = choice
        self.accept()


def ask(parent, title, intro, lines, skip_text, anyway_text):
    """Show the dialog; returns (choice, dont_ask_again). Tests replace this."""
    d = ChoiceDialog(parent, title, intro, lines, skip_text, anyway_text)
    d.exec()
    return d.choice, d.dont_ask.isChecked()


# ------------------------------------------------------------------ duplicates
def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def job_signature(job) -> str:
    """The job as comparable text: its sources and every setting, minus the
    fields that don't change what is made (the preview title)."""
    d = J.job_to_dict(job)
    d.pop("title_text", None)
    if "source" in d:
        d["source"] = _norm(d["source"])
    if "clip_paths" in d:
        d["clip_paths"] = [_norm(p) for p in d["clip_paths"]]
    return json.dumps(d, sort_keys=True, default=str)


def find_duplicates(jobs, items):
    """[(job, message)] for each new job that matches one still listed in the queue."""
    listed = {}
    for it in items:
        if it.status not in _NOT_DONE:
            listed.setdefault(job_signature(it.job), it)
    out = []
    for job in jobs:
        it = listed.get(job_signature(job))
        if it is None:
            continue
        outs = [Path(o).name for o in (it.result.outputs if it.result else [])]
        if it.status in ("done", "reverted") and outs:
            more = f" and {len(outs) - 1} more" if len(outs) > 1 else ""
            out.append((job, f"{job.title}: finished, saved as {outs[0]}{more}"))
        elif it.status in ("done", "reverted"):
            out.append((job, f"{job.title}: finished"))
        else:
            out.append((job, f"{job.title}: already in the queue"))
    return out


# ------------------------------------------------------------ already processed
def find_processed(jobs, infos):
    """[(job, message)] for each source file that already went through the
    kind of work the job would do. `infos` maps source path -> MediaInfo
    (files without one are judged by their name only)."""
    out = []
    for job in jobs:
        for src in job.sources():
            why = already_done(job, src, infos.get(_norm(src)))
            if why:
                out.append((job, f"{Path(src).name}: {why}"))
    return out


# ------------------------------------------------------------------- entry point
def check_new_jobs(main, jobs, infos, parent=None, check_processed=True):
    """Run both warnings. `infos` is {source path: MediaInfo} from what the tab
    already probed. `check_processed=False` leaves out the "already processed" warning (Replay merge's
    internal "merge, then send to Clips" jobs). Returns the jobs to queue ([] = nothing, e.g. cancelled)."""
    jobs = list(jobs)
    s = main.settings
    if s.warn_duplicate_jobs and jobs:
        dups = find_duplicates(jobs, main.queue.items)
        if dups:
            choice, dont_ask = ask(
                parent, "Already queued or done",
                "These jobs match one already in the queue list (same files, same settings):",
                [m for _, m in dups], "Skip duplicates", "Add anyway")
            if dont_ask:
                s.warn_duplicate_jobs = False
                s.save()
            if choice == "cancel":
                return []
            if choice == "skip":
                gone = {id(j) for j, _ in dups}
                jobs = [j for j in jobs if id(j) not in gone]
    if s.warn_already_processed and check_processed and jobs:
        infos = {_norm(k): v for k, v in infos.items()}
        done = find_processed(jobs, infos)
        if done:
            choice, dont_ask = ask(
                parent, "Looks already processed",
                "Trimmer's own tag or file names say these files already went through the same kind of "
                "work. Skipping a merge leaves out its whole session.",
                [m for _, m in done], "Skip these", "Process anyway")
            if dont_ask:
                s.warn_already_processed = False
                s.save()
            if choice == "cancel":
                return []
            if choice == "skip":
                gone = {id(j) for j, _ in done}
                jobs = [j for j in jobs if id(j) not in gone]
    return jobs
