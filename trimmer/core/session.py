"""Save state: what was open in the Clips tab, Replay merge and the queue
when Trimmer was closed, as one JSON file that is read once on the next start.
"""

import json
import os

from .util import app_data_dir

SESSION_FILE = "session.json"
VERSION = 1


def session_path():
    return app_data_dir() / SESSION_FILE


def save_session(state: dict) -> bool:
    p = session_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, **state}, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True
    except (OSError, TypeError, ValueError):
        return False


def load_session():
    """The saved state, or None when there is none or it can't be read."""
    try:
        data = json.loads(session_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def clear_session():
    try:
        session_path().unlink()
    except OSError:
        pass
