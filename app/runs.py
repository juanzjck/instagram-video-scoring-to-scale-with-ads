"""Runs: one pipeline execution with a fixed (prompt version, provider, model), recorded on the session.

Start pipeline on a session with no completed run runs in place and records the run in session.json under
"run". On a session that already has a completed run it creates a new child session (a collection child;
the original becomes run 001 of an auto-created parent) and runs there. A run that did not finish
(some videos failed, or the server stopped) is resumed in place with the same prompt and model.

A child starts honest: it copies the dataset, clips, keyframes and transcripts, and the locked ground truth, but
no score caches, iterations, recaps, timings, review sheets or decisions. Vision features are reused only when
the new run's provider + model are the same as the run it branches from (vision does not depend on the prompt).
Views (video list, card, Evaluation) default to the session's own run prompt; the Dataset tab's prompt selector
only chooses the prompt of the NEXT run. A session saved before runs were recorded is inferred on read from its
score caches and never rewritten by that.
"""

import json
import threading
import uuid
from datetime import datetime, timezone

from . import collection, config, dataset, ingest, jobs, pipeline
from . import prompt_versions as pv


class Conflict(Exception):
    """The request is valid but the state refuses it (HTTP 409)."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _session_file(sid):
    return config.UPLOADS / sid / dataset.SESSION_FILE


def _read(sid):
    return json.loads(_session_file(sid).read_text(encoding="utf-8"))


def _patch(sid, fn):
    """Atomic read-modify-write of one session.json."""
    p = _session_file(sid)
    s = json.loads(p.read_text(encoding="utf-8"))
    fn(s)
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex[:8]}.tmp")        # unique per write: concurrent writers never share a temp file
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def infer():
    """A run read off the open session's score caches (the model it is set to): the prompt that scored the most
    videos, the active one on a tie. None when nothing was scored. Never written."""
    videos = list(ingest.load_inputs()[0])
    llm = config.llm_choice()
    best = None
    for v in pv.listing()["versions"]:
        if v["id"] == pv.BASELINE:
            continue
        n = sum(pipeline._exists(vid, pv.stage(v["id"])) for vid in videos)
        if n and (best is None or n > best[1] or (n == best[1] and v["id"] == pv.active())):
            best = (v["id"], n)
    if best is None:
        return None
    return {"n": 1, "prompt_id": best[0], "provider": llm["provider"], "model": llm["model"],
            "status": "done" if best[1] == len(videos) else "partial", "scored": best[1], "videos": len(videos),
            "errors": None, "inferred": True}


def current():
    """The open session's run: the recorded one, else an inferred one, else None."""
    if config.DATA is None:
        return None
    s = json.loads((config.DATA / dataset.SESSION_FILE).read_text(encoding="utf-8"))
    return s.get("run") or infer()


def view_prompt():
    """The prompt the open session's views default to: its run's, if that version still exists; else None (active)."""
    try:
        r = current()
        if r and r.get("prompt_id"):
            pv.get(r["prompt_id"])
            return r["prompt_id"]
    except (KeyError, ValueError, OSError):
        pass
    return None


_start_lock = threading.Lock()         # one Start at a time: running check -> child creation -> job start


def _completed(run):
    return bool(run) and run.get("status") == "done"


def _same_setup(run, pid, llm):
    return bool(run) and (run.get("prompt_id"), run.get("provider"), run.get("model")) == (pid, llm["provider"], llm["model"])


def _label(pid, llm):
    return f"{pid} on {llm['provider']} / {llm['model']}"


def _siblings():
    """[(index, session id, run record)] of the open session's collection, recorded runs only."""
    s = json.loads((config.DATA / dataset.SESSION_FILE).read_text(encoding="utf-8"))
    mark = s.get("collection")
    if not mark:
        return [(1, s["id"], s.get("run"))] if s.get("run") else []
    return collection.run_records(mark["id"])


def plan(prompt_id=None):
    """What Start pipeline would do, with the text of its confirm dialog. No model call, nothing written."""
    if config.DATA is None:
        raise ValueError("open a campaign first")
    if prompt_id == pv.BASELINE:
        raise ValueError("P5 is scored in an iteration, not in a pipeline run")
    pid = prompt_id or pv.active()
    pv.get(pid)                                                # KeyError for an unknown id
    llm = config.llm_choice()
    run = current()
    n_videos = len(ingest.load_inputs()[0])
    out = {"prompt_id": pid, "provider": llm["provider"], "model": llm["model"], "videos": n_videos, "run": run,
           "vision_reuse": False, "repeat_of": []}
    what = _label(pid, llm)
    if run is None:
        out |= {"action": "first", "next_n": 1, "button": "Start first run",
                "message": f"Start the first run of this session with {what}. This makes model calls for {n_videos} videos "
                           f"(vision and scoring)."}
    elif not _completed(run):
        old = _label(run["prompt_id"], run)
        if _same_setup(run, pid, llm):
            out |= {"action": "resume", "next_n": run.get("n", 1), "button": f"Resume run {run.get('n', 1):03d}",
                    "message": f"Run {run.get('n', 1):03d} ({old}) did not finish: {run.get('scored')} of {run.get('videos')} videos "
                               f"scored. Resume it in place? Videos already scored are reused; the rest make model calls."}
        else:
            nxt = collection_next(run)
            out |= {"action": "ask", "next_n": nxt, "button": "Start pipeline",
                    "message_resume": f"Run {run.get('n', 1):03d} ({old}) did not finish. Resume it with {old}? "
                                      f"(Cancel to see the other option.)",
                    "message_child": f"Create run {nxt:03d} with {what} instead, leaving the unfinished run as it is?"}
    else:
        nxt = collection_next(run)
        reuse = run.get("provider") == llm["provider"] and run.get("model") == llm["model"]
        same = [i for i, _, r in _siblings() if _same_setup(r, pid, llm)]
        out |= {"action": "child", "next_n": nxt, "vision_reuse": reuse, "repeat_of": same,
                "button": f"Create run {nxt:03d} and start",
                "message": f"This session already has a completed run ({_label(run['prompt_id'], run)}). Create run {nxt:03d} "
                           f"with {what}? It copies the dataset, clips and ground truth, not earlier scores or reviews. "
                           + ("Vision features are reused (same model): only scoring makes model calls."
                              if reuse else f"Vision is re-run on the new model: model calls for {n_videos} videos (vision and scoring).")
                           + (f" Same prompt and model as run {', '.join(f'{i:03d}' for i in same)}: a repeat run." if same else "")}
    return out


def collection_next(run):
    """The index the next child of this session's collection gets (2 when it is not in one yet)."""
    s = json.loads((config.DATA / dataset.SESSION_FILE).read_text(encoding="utf-8"))
    mark = s.get("collection")
    return collection.next_index(mark["id"]) if mark else 2


def _start_job(pid, force, extra):
    """Record the run (status running) and start the job in the open session. False when a job is running."""
    sid, llm = config.DATASET_ID, config.llm_choice()
    videos = list(ingest.load_inputs()[0])
    rec = {"n": extra["n"], "prompt_id": pid, "prompt_sha256": pv.get(pid)["sha256"], "provider": llm["provider"],
           "model": llm["model"], "started_at": _now(), "finished_at": None, "status": "running", "scored": 0,
           "videos": len(videos), "errors": 0, "vision_reused": extra.get("vision_reused", False),
           "repeat_of": extra.get("repeat_of", [])}

    def finish(job):
        vs = job.get("videos") or {}
        scored = sum(1 for v in vs.values() if v.get("state") == "done" and v.get("verdict"))
        errors = sum(1 for v in vs.values() if v.get("state") == "error")
        done = scored == len(videos) and errors == 0 and not job.get("finish_error")
        _patch(sid, lambda s: s.__setitem__("run", dict(rec, finished_at=_now(), scored=scored, errors=errors,
                                                        status="done" if done else "partial")))
    prev = _read(sid).get("run")

    def restore(s):
        s.pop("run", None) if prev is None else s.__setitem__("run", prev)
    _patch(sid, lambda s: s.__setitem__("run", rec))
    try:
        started = jobs.start(videos, force=force, prompt_id=pid, on_finish=finish)
    except BaseException:
        _patch(sid, restore)
        raise
    if not started:
        _patch(sid, restore)                                   # no "running" record for a job that never began
        return False
    return rec


def start(prompt_id=None, force=False, mode="auto"):
    """Start the pipeline per plan(). Returns {"session_id", "created_child", "run"} after the job has started.
    A second Start while one is in progress gets a Conflict (one lock covers the check, the child and the job start)."""
    if not _start_lock.acquire(blocking=False):
        raise Conflict("a start is already in progress")
    try:
        return _start(prompt_id, force, mode)
    finally:
        _start_lock.release()


def _start(prompt_id, force, mode):
    p = plan(prompt_id)
    if jobs.running():
        raise Conflict("the pipeline is already running")
    pid, run, act = p["prompt_id"], p["run"], p["action"]
    if act == "ask" and mode == "auto":
        raise Conflict("this session has an unfinished run with another prompt or model: choose to resume it or create a new run")
    if mode == "child" and run is None:
        raise Conflict("there is no run to branch from yet")
    if mode == "resume" and (run is None or _completed(run)):
        raise Conflict("there is no unfinished run to resume")
    if mode == "child" or (mode == "auto" and act == "child"):
        return _start_child(p, force)
    if mode == "resume" and act == "ask":                      # resume with the unfinished run's own prompt and model
        pid = run["prompt_id"]
        dataset.set_llm({"provider": run["provider"], "model": run["model"]})
    n = (run or {}).get("n", 1) if act in ("resume", "ask") else 1
    rec = _start_job(pid, force, {"n": n})
    if not rec:
        raise Conflict("the pipeline is already running")
    return {"session_id": config.DATASET_ID, "created_child": False, "run": rec}


def _start_child(p, force):
    src = config.DATASET_ID
    llm = config.llm_choice()
    run = p["run"]
    cid, made = collection.ensure_for(src)
    child = None
    prev_llm = _read(src).get("llm")
    try:
        keep = llm if (run.get("provider"), run.get("model")) == (llm["provider"], llm["model"]) else None
        child, index = collection.add_run(cid, src, keep)
        if run.get("provider") and not run.get("inferred"):    # the source stays on the model its run used
            _patch(src, lambda s: s.__setitem__("llm", {"provider": run["provider"], "model": run["model"]}))
        dataset._activate(child)
        rec = _start_job(p["prompt_id"], force, {"n": index, "vision_reused": bool(keep), "repeat_of": p["repeat_of"]})
        if not rec:
            raise Conflict("the pipeline is already running")
    except BaseException:
        dataset._activate(src)
        _patch(src, lambda s: s.pop("llm", None) if prev_llm is None else s.__setitem__("llm", prev_llm))   # undo the model reset
        if child:
            collection.remove_run(cid, child)
        if made:
            collection.discard_if_empty(cid)
        raise
    return {"session_id": child, "created_child": True, "run": rec}
