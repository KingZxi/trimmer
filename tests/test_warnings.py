"""The two queue warnings: "already queued or done" and "looks already processed"."""

from types import SimpleNamespace

import pytest

from tests import fixtures as fx
from tests.gui_helpers import WORK, pump, window  # also sets up the offscreen app and data folder
from trimmer.core.jobs import ClipJob, JobContext, JobOptions, JobResult, MergeJob, run_job
from trimmer.core.settings import AppSettings
from trimmer.gui import queue_checks as qc


@pytest.fixture(autouse=True)
def warnings_on():
    """"Don't ask again" saves to the settings file; start and leave every test with both on."""
    def reset():
        st = AppSettings.load()
        st.warn_duplicate_jobs = st.warn_already_processed = True
        st.save()
    reset()
    yield
    reset()


class Asker:
    """Stands in for the dialog: records what it was shown, answers as told."""

    def __init__(self, choice, dont_ask=False):
        self.choice, self.dont_ask, self.shown = choice, dont_ask, []

    def __call__(self, parent, title, intro, lines, skip_text, anyway_text):
        self.shown.append((title, list(lines)))
        return self.choice, self.dont_ask


def use(monkeypatch, asker):
    monkeypatch.setattr(qc, "ask", asker)
    return asker


def clip_window(out, src):
    w = window(out)
    w.queue.auto_start.setChecked(False)
    w._route([str(src)])
    pump(15, lambda: w.clips.entries)
    w.clips.table.selectRow(0)
    pump(1)
    return w


def test_duplicate_clip_job_warning(monkeypatch):
    (WORK / "wsrc").mkdir(exist_ok=True)
    src = fx.single_clip(WORK / "wsrc")
    w = clip_window(WORK / "wout1", src)
    q = w.queue
    w.clips.entries[0].segments = [(1.0, 5.0)]
    w.clips.queue()
    assert len(q.items) == 1
    # Skip duplicates (the default button): nothing is added.
    a = use(monkeypatch, Asker("skip"))
    w.clips.queue()
    assert len(q.items) == 1
    assert a.shown[0][0] == "Already queued or done"
    assert a.shown[0][1] == [f"{src.name}: already in the queue"]
    # Different settings are not a duplicate.
    w.clips.entries[0].segments = [(1.0, 6.0)]
    a = use(monkeypatch, Asker("skip"))
    w.clips.queue()
    assert len(q.items) == 2 and not a.shown
    # Cancel adds nothing; Add anyway keeps the duplicate.
    use(monkeypatch, Asker("cancel"))
    w.clips.queue()
    assert len(q.items) == 2
    use(monkeypatch, Asker("anyway"))
    w.clips.queue()
    assert len(q.items) == 3
    # A finished job says where its output went.
    q.items[1].status = "done"
    q.items[1].result = JobResult(outputs=[str(WORK / "wout1" / "Gameplay (trimmed).mp4")])
    q.items[2].status = "failed"            # failed jobs don't count: queueing again is a retry
    a = use(monkeypatch, Asker("skip"))
    w.clips.queue()
    assert a.shown[0][1] == [f"{src.name}: finished, saved as Gameplay (trimmed).mp4"]
    assert len(q.items) == 3
    # "Don't ask again" turns the setting off and saves it.
    use(monkeypatch, Asker("anyway", dont_ask=True))
    w.clips.queue()
    assert w.settings.warn_duplicate_jobs is False
    assert AppSettings.load().warn_duplicate_jobs is False
    a = use(monkeypatch, Asker("skip"))
    w.clips.queue()
    assert not a.shown and len(q.items) == 5
    w.close()


def test_duplicate_merge_job_warning(monkeypatch):
    fx.shared_replays(WORK / "wrep")
    w = window(WORK / "wout2")
    w.queue.auto_start.setChecked(False)
    w.tabs.setCurrentWidget(w.merge)
    w._route([str(WORK / "wrep")])
    pump(30, lambda: len(w.merge.sessions) == 2)
    w.merge.queue()
    sigs = lambda: [qc.job_signature(i.job) for i in w.queue.items]       # noqa: E731
    assert len(sigs()) >= 1
    a = use(monkeypatch, Asker("skip"))
    w.merge.queue()
    assert a.shown and all(m.endswith("already in the queue") for m in a.shown[0][1])
    assert len(set(sigs())) == len(sigs())          # no duplicate was added
    before = len(sigs())
    use(monkeypatch, Asker("anyway"))
    w.merge.queue()
    assert len(sigs()) > before and len(set(sigs())) < len(sigs())
    w.close()


def test_already_processed_warning_for_clips(monkeypatch):
    (WORK / "wsrc2").mkdir(exist_ok=True)
    base = fx.single_clip(WORK / "wsrc2")
    made = WORK / "wmade"
    made.mkdir(exist_ok=True)
    opts = JobOptions(output_dir=str(made), conflict_policy="rename")
    tagged = run_job(ClipJob(str(base), segments=[(1.0, 9.0)], options=opts), JobContext()).outputs[0]
    w = clip_window(WORK / "wout3", tagged)
    entry = w.clips.entries[0]
    assert entry.info.trimmer_tag == "Trimmer: trimmed"
    # Trimming a trimmed file warns; Skip these drops the job.
    entry.segments = [(1.0, 4.0)]
    a = use(monkeypatch, Asker("skip"))
    w.clips.queue()
    assert not w.queue.items
    assert a.shown[0][0] == "Looks already processed"
    assert "already trimmed (Trimmer: trimmed)" in a.shown[0][1][0]
    # Process anyway queues it.
    use(monkeypatch, Asker("anyway"))
    w.clips.queue()
    assert len(w.queue.items) == 1
    # Compressing a file that was only trimmed is fine: no dialog.
    entry.segments = []
    w.clips.compress.setChecked(True)
    a = use(monkeypatch, Asker("skip"))
    w.clips.queue()
    assert not a.shown and len(w.queue.items) == 2
    # Don't ask again flips the setting.
    entry.segments = [(1.0, 3.0)]
    w.clips.compress.setChecked(False)
    use(monkeypatch, Asker("skip", dont_ask=True))
    w.clips.queue()
    assert w.settings.warn_already_processed is False
    assert AppSettings.load().warn_already_processed is False
    w.close()


def test_already_processed_by_name_and_merge(monkeypatch, tmp_path):
    main = SimpleNamespace(settings=AppSettings(), queue=SimpleNamespace(items=[]))
    main.settings.save = lambda: None
    paths = {}
    for key, name in (("named", "Highlight (trimmed).mp4"), ("plain", "Plain.mp4"),
                      ("merged", "Merged 2026-10-20 20-00-00 to 20-00-48.mp4"),
                      ("lossy", "Clip (LOSSY), (CRF23, H265, medium).mp4")):
        paths[key] = tmp_path / name
        paths[key].write_bytes(b"x")
    jobs = [ClipJob(str(paths["named"]), segments=[(0.0, 1.0)]),
            ClipJob(str(paths["plain"]), segments=[(0.0, 1.0)]),
            ClipJob(str(paths["lossy"]), compress=True),
            ClipJob(str(paths["named"]), compress=True),      # compressing a trimmed file is fine
            MergeJob([str(paths["merged"]), str(paths["plain"])])]
    a = use(monkeypatch, Asker("skip"))
    left = qc.check_new_jobs(main, jobs, {})
    assert left == [jobs[1], jobs[3]]
    shown = a.shown[0][1]
    assert len(shown) == 3 and shown[0].startswith("Highlight (trimmed).mp4: its name says")
    assert any(m.startswith("Merged 2026") for m in shown)
    # With the setting off nothing is asked.
    main.settings.warn_already_processed = False
    a = use(monkeypatch, Asker("skip"))
    assert qc.check_new_jobs(main, jobs, {}) == jobs and not a.shown
