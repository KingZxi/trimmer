"""The saved queue survives a crash mid-save and two saves at once."""

import json
import threading

import pytest

from trimmer.core import journal


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIMMER_DATA_DIR", str(tmp_path))
    return tmp_path


def test_a_damaged_queue_file_falls_back_to_the_previous_copy(data):
    journal.save_queue([{"source": "a.mp4"}])
    journal.save_queue([{"source": "a.mp4"}, {"source": "b.mp4"}])
    assert len(journal.load_queue()) == 2
    # A crash or power cut while the file was being written left it empty or cut off.
    for damage in ("", '[{"source": "a.m'):
        (data / "queue.json").write_text(damage, encoding="utf-8")
        assert journal.load_queue() == [{"source": "a.mp4"}]


def test_clearing_the_queue_also_forgets_the_previous_copy(data):
    journal.save_queue([{"source": "a.mp4"}])
    journal.save_queue([{"source": "b.mp4"}])
    journal.clear_queue()
    assert journal.load_queue() == []
    assert not list(data.glob("queue.json*"))


def test_saves_from_two_threads_leave_a_whole_file(data):
    def save(n):
        for i in range(40):
            journal.save_queue([{"source": f"{n}-{i}.mp4"}] * 50)
    threads = [threading.Thread(target=save, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    saved = json.loads((data / "queue.json").read_text(encoding="utf-8"))
    assert len(saved) == 50
    assert not list(data.glob("*.tmp"))
