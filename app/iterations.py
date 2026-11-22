"""Iterations: run one prompt version over the whole campaign, next to the P5 baseline, and freeze the result.

An iteration is one turn of the Section 6 loop: score every video with a prompt version (and with P5,
on the same provider + model), freeze each video's final verdicts in runs/iterations/<n>.json, and
measure both against the locked ground truth (evaluate.compare_iteration, the only place answer keys
are read). The adoption gate reads the latest finished iteration of a version on the current model:
a version is adopted only when it beats P5 there (prompt_versions.set_active).
"""

import json
from datetime import datetime, timezone

from . import config, evaluate, ingest, jobs, pipeline
from . import prompt_versions as pv


def _dir():
    if config.RUNS is None:
        raise ValueError("open a campaign first")
    return config.RUNS / "iterations"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(rec, folder=None):
    d = folder or _dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{rec['n']:04d}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _records():
    d = _dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return out


def start(prompt_id, force=False, note=""):
    """Start an iteration in the background. Returns the job snapshot; False when a job is already running."""
    if prompt_id == pv.BASELINE:
        raise ValueError("P5 is the baseline every iteration already runs; pick a P6-family version")
    v = pv.get(prompt_id)                    # KeyError for an unknown id
    videos = list(ingest.load_inputs()[0])
    if not videos:
        raise ValueError("generate the campaign's dataset first")
    folder = _dir()
    n = max((r["n"] for r in _records()), default=0) + 1
    rec = {"n": n, "status": "running", "prompt_id": prompt_id, "prompt_sha256": v["sha256"],
           "parent": v["parent"], "baseline": pv.BASELINE, "llm": config.llm_choice(), "force": bool(force),
           "note": (note or "").strip()[:500], "started_at": _now(), "finished_at": None, "videos": {}}

    def finish(job):
        out = {}
        for vid in videos:
            c = pipeline.load(vid, pv.stage(prompt_id)) or {}
            b = pipeline.load(vid, "score_p5") or {}
            if c and c.get("prompt_sha256") not in (None, v["sha256"]):
                c = {}                       # never count an answer from a different prompt text
            out[vid] = {"final": c.get("verdict"), "model_verdict": c.get("model_verdict"), "score": c.get("score"),
                        "rule_override": c.get("rule_override"),
                        "p5_final": b.get("verdict"), "p5_model": b.get("model_verdict"), "p5_score": b.get("score"),
                        "error": (job["videos"].get(vid) or {}).get("error")}
        rec.update(status="done", finished_at=_now(), videos=out,
                   errors=sum(1 for x in out.values() if x["error"]))
        _write(rec, folder)

    _write(rec, folder)
    if not jobs.start(videos, force=force, prompt_id=prompt_id, baseline=True, on_finish=finish, iteration=n):
        (folder / f"{n:04d}.json").unlink(missing_ok=True)
        return False
    return jobs.snapshot()


def _view(rec):
    """A stored record plus its comparison with P5, measured now against the ground truth."""
    out = dict(rec)
    if rec.get("status") == "running" and not (jobs.running() and jobs.snapshot().get("iteration") == rec["n"]):
        out["status"] = "interrupted"        # the server stopped mid-run; nothing was frozen
    out["comparison"] = evaluate.compare_iteration(rec.get("videos") or {}) if out["status"] == "done" else None
    out["prompt_exists"] = any(x["id"] == rec["prompt_id"] for x in pv.listing()["versions"])
    return out


def listing():
    return [_view(r) for r in reversed(_records())]         # newest first


def get(n):
    for r in _records():
        if r["n"] == n:
            return _view(r)
    raise KeyError(n)


def adoption_check(prompt_id):
    """The gate's answer for adopting prompt_id on the current provider + model:
    {"beats_baseline": bool, "why": str, "iteration": n or None}."""
    v = pv.get(prompt_id)
    choice = config.llm_choice()
    done = [r for r in _records() if r.get("status") == "done" and r["prompt_id"] == prompt_id
            and r.get("prompt_sha256") == v["sha256"] and r.get("llm") == choice]
    if not done:
        return {"beats_baseline": False, "iteration": None,
                "why": f"no finished iteration of {prompt_id} on {choice['provider']} / {choice['model']}"}
    last = done[-1]
    c = evaluate.compare_iteration(last.get("videos") or {})
    return {"beats_baseline": bool(c["beats_baseline"]), "iteration": last["n"],
            "why": f"iteration #{last['n']}: {c['why']}"}
