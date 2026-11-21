"""The "Start pipeline" job: runs every video of the active dataset in a background thread
so the UI can poll progress instead of holding one request open for minutes.

The same runner does prompt-version iterations (app/iterations.py): kind "iteration" scores each
video with the chosen prompt and with the P5 baseline, then calls on_finish to freeze the results.
Only one job runs at a time, whichever kind."""

import copy
import threading
import time
from datetime import datetime, timezone

from . import pipeline

_lock = threading.Lock()
_state = {"running": False, "total": 0, "done": 0, "videos": {}, "kind": "pipeline", "prompt_id": None,
          "iteration": None, "started_at": None, "finished_at": None, "elapsed_s": None}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def snapshot():
    with _lock:
        return copy.deepcopy(_state)


def running():
    with _lock:
        return _state["running"]


def reset():
    """Forget the last run; called when the dataset changes. No-op while a job runs."""
    with _lock:
        if not _state["running"]:
            _state.update(total=0, done=0, videos={}, kind="pipeline", prompt_id=None, iteration=None,
                          started_at=None, finished_at=None, elapsed_s=None)


def _set(video_id, **fields):
    with _lock:
        _state["videos"][video_id].update(fields)


def _run(video_ids, force, prompt_id, baseline, on_finish):
    run_t0 = time.perf_counter()
    for vid in video_ids:
        t0 = time.perf_counter()
        try:
            card = pipeline.process(vid, force=force, progress=lambda step: _set(vid, state=step), prompt_id=prompt_id)
            if baseline:
                _set(vid, state="scoring P5 baseline")
                pipeline.run_p5(vid, force=force)
            verdict = (pipeline.load(vid, pipeline.pv.stage(prompt_id)) or {}).get("verdict") if prompt_id \
                else (card.get("score") or {}).get("verdict")
            _set(vid, state="done", verdict=verdict)
        except Exception as e:                       # one bad video must not stop the rest
            _set(vid, state="error", error=f"{type(e).__name__}: {e}")
        _set(vid, elapsed_s=round(time.perf_counter() - t0, 1))   # cached stages make this shorter than a fresh run
        with _lock:
            _state["done"] += 1
    if on_finish:
        try:
            on_finish(snapshot())
        except Exception as e:                       # recorded, never lost silently
            with _lock:
                _state["finish_error"] = f"{type(e).__name__}: {e}"
    with _lock:
        _state.update(running=False, finished_at=_now(), elapsed_s=round(time.perf_counter() - run_t0, 1))


def start(video_ids, force=False, prompt_id=None, baseline=False, on_finish=None, iteration=None):
    """Returns False when a job is already running.

    prompt_id scores with that prompt version instead of the active one; baseline also scores P5;
    on_finish(snapshot) runs in the job thread after the last video (an iteration freezes its results)."""
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, total=len(video_ids), done=0, started_at=_now(), finished_at=None,
                      elapsed_s=None, videos={v: {"state": "queued"} for v in video_ids},
                      kind="iteration" if iteration else "pipeline", prompt_id=prompt_id, iteration=iteration)
        _state.pop("finish_error", None)
    threading.Thread(target=_run, args=(list(video_ids), force, prompt_id, baseline, on_finish), daemon=True).start()
    return True
