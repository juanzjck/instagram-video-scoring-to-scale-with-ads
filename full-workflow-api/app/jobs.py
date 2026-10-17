"""The "Start pipeline" job: runs every video of the active dataset in a background thread
so the UI can poll progress instead of holding one request open for minutes."""

import copy
import threading
from datetime import datetime, timezone

from . import pipeline

_lock = threading.Lock()
_state = {"running": False, "total": 0, "done": 0, "videos": {},
          "started_at": None, "finished_at": None}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def snapshot():
    with _lock:
        return copy.deepcopy(_state)


def running():
    with _lock:
        return _state["running"]


def _set(video_id, **fields):
    with _lock:
        _state["videos"][video_id].update(fields)


def _run(video_ids, force):
    for vid in video_ids:
        try:
            card = pipeline.process(vid, force=force, progress=lambda step: _set(vid, state=step))
            _set(vid, state="done", verdict=(card.get("score") or {}).get("verdict"))
        except Exception as e:                       # one bad video must not stop the rest
            _set(vid, state="error", error=f"{type(e).__name__}: {e}")
        with _lock:
            _state["done"] += 1
    with _lock:
        _state.update(running=False, finished_at=_now())


def start(video_ids, force=False):
    """Returns False when a job is already running."""
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, total=len(video_ids), done=0, started_at=_now(),
                      finished_at=None, videos={v: {"state": "queued"} for v in video_ids})
    threading.Thread(target=_run, args=(list(video_ids), force), daemon=True).start()
    return True
