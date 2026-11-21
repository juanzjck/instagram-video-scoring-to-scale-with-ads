"""Offline QA tests for scripts/run_collection_test.py: the parent's scheduling (resume, retries, the
usage-limit stop, concurrent summary writes, shared vision copies) and the worker's fresh-scoring,
fail-fast and read-only guarantees. No `claude` binary, key or network: the worker subprocess is
replaced by a fake _spawn, and the CLI by the FakeCLI of test_claude_code.py.
"""

import hashlib
import json
import random
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app import collection, config, pipeline
from tests.test_claude_code import (SRC, VIDEOS, _digest, _events, _harness, _scorer,  # noqa: F401
                                    cli, uploads)

MODEL = "claude-opus-5-5"
SLUG = pipeline.llm_slug({"provider": "claude_code", "model": MODEL})


def _args(**kw):
    base = dict(source=SRC, name="QA", children=5, concurrency=1, limit=None, provider="claude_code",
                model=MODEL, vision="shared", prompt=None, skip_errors=False)
    return SimpleNamespace(**{**base, **kw})


class FakeSpawn:
    """Stands in for the worker subprocess. outcome(child, n_th_call) -> result dict."""

    def __init__(self, outcome=None, delay=0.0):
        self.calls, self.lock, self.delay = [], threading.Lock(), delay
        self.outcome = outcome or (lambda child, i: {"status": "done", "videos": {}, "comparison": None,
                                                     "gate": {"beats_baseline": True}, "calls": [],
                                                     "elapsed_s": 1.0, "rate_limited": False, "error": None})
        self.active = 0
        self.max_active = 0

    def __call__(self, child, args, phase, log):
        if phase == "features":                       # what the features worker leaves in the first run
            for v in VIDEOS:
                d = config.UPLOADS / child / "runs" / v / "llm" / SLUG
                d.mkdir(parents=True, exist_ok=True)
                (d / "features.json").write_text(json.dumps({"hook_strength": "strong", "v": v}))
            return {"status": "done", "elapsed_s": 2.0, "features_usage": {}}
        with self.lock:
            self.calls.append(child)
            i = len(self.calls)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if self.delay:
            time.sleep(random.uniform(0, self.delay))
        try:
            return self.outcome(child, i)
        finally:
            with self.lock:
                self.active -= 1


def _summary(cid):
    return json.loads((collection.folder(cid) / "test_summary.json").read_text(encoding="utf-8"))


def _install(h, monkeypatch, fake):
    monkeypatch.setattr(h, "_spawn", fake)
    return fake


# ---------------------------------------------------------------- parent: resume, retries, naming

def test_resume_skips_done_retries_failed_and_interrupted_without_new_children(uploads, monkeypatch):
    h = _harness()
    fake = _install(h, monkeypatch, FakeSpawn())
    h.run(_args(limit=2))                                         # the pilot
    cid = collection.find("QA", SRC)["id"]
    kids = collection.get(cid)["child_ids"]
    assert len(kids) == 5 and fake.calls == kids[:2]
    s = _summary(cid)
    assert [s["children"][k]["status"] for k in kids[:2]] == ["done", "done"]
    assert [s["children"][k]["index"] for k in kids[:2]] == [1, 2]

    # a crash leaves run 2 "running" and a failed attempt leaves run 3 "error"
    s["children"][kids[1]]["status"] = "running"
    s["children"][kids[2]] = {"index": 3, "status": "error", "error": "LLMError: boom"}
    (collection.folder(cid) / "test_summary.json").write_text(json.dumps(s))

    fake.calls.clear()
    h.run(_args())
    assert sorted(fake.calls) == sorted(kids[1:])                 # run 1 skipped, 2-5 (re)run once each
    assert collection.get(cid)["child_ids"] == kids               # resuming made no new runs
    s = _summary(cid)
    assert all(s["children"][k]["status"] == "done" for k in kids)
    assert [s["children"][k]["index"] for k in kids] == [1, 2, 3, 4, 5]
    assert [b["runs"] for b in s["batches"]] == [2, 4]
    names = [json.loads((uploads / k / "session.json").read_text())["name"] for k in kids]
    assert names == [f"QA · run {i:03d}" for i in range(1, 6)]

    fake.calls.clear()
    h.run(_args())                                                # nothing left to do
    assert fake.calls == []


def test_skip_errors_leaves_failed_runs_alone(uploads, monkeypatch):
    h = _harness()
    fake = _install(h, monkeypatch, FakeSpawn(lambda c, i: {"status": "error", "error": "LLMError: x",
                                                            "rate_limited": False, "elapsed_s": 1}))
    h.run(_args(children=3))
    assert len(fake.calls) == 3
    fake.calls.clear()
    h.run(_args(children=3, skip_errors=True))
    assert fake.calls == []


def test_growing_the_collection_appends_runs_with_the_next_indices(uploads, monkeypatch):
    h = _harness()
    _install(h, monkeypatch, FakeSpawn())
    h.run(_args(children=2))
    h.run(_args(children=4))
    cid = collection.find("QA", SRC)["id"]
    kids = collection.get(cid)["child_ids"]
    idx = [json.loads((uploads / k / "session.json").read_text())["collection"]["index"] for k in kids]
    assert len(kids) == 4 and idx == [1, 2, 3, 4] and len(set(kids)) == 4


def test_a_different_llm_on_the_same_collection_is_refused(uploads, monkeypatch):
    h = _harness()
    _install(h, monkeypatch, FakeSpawn())
    h.run(_args(children=1))
    with pytest.raises(SystemExit, match="use another --name"):
        h.run(_args(children=1, model="claude-sonnet-5-5"))


# ---------------------------------------------------------------- parent: usage-limit stop

def test_usage_limit_stops_new_runs_and_the_next_start_resumes(uploads, monkeypatch):
    h = _harness()

    def outcome(child, i):
        if i == 2:
            return {"status": "error", "error": "DTe: RateLimited: limit", "rate_limited": True, "elapsed_s": 1}
        return {"status": "done", "videos": {}, "comparison": None, "gate": {}, "calls": [], "elapsed_s": 1,
                "rate_limited": False, "error": None}
    fake = _install(h, monkeypatch, FakeSpawn(outcome))
    h.run(_args(children=6, concurrency=1))
    cid = collection.find("QA", SRC)["id"]
    kids = collection.get(cid)["child_ids"]
    assert fake.calls == kids[:2]                                 # nothing started after the limit
    s = _summary(cid)
    assert s["batches"][-1]["stopped_on_limit"] is True
    assert s["children"][kids[1]]["status"] == "error" and s["children"][kids[1]]["rate_limited"] is True
    assert all(k not in s["children"] for k in kids[2:])          # untouched, not marked failed

    fake.calls.clear()
    fake.outcome = lambda c, i: {"status": "done", "videos": {}, "gate": {}, "calls": [], "elapsed_s": 1}
    h.run(_args(children=6, concurrency=1))
    assert fake.calls == kids[1:]                                 # the limited run is retried, then the rest


# ---------------------------------------------------------------- parent: concurrency

def test_concurrent_runs_never_lose_a_summary_update(uploads, monkeypatch):
    h = _harness()
    fake = _install(h, monkeypatch, FakeSpawn(delay=0.05))
    h.run(_args(children=16, concurrency=4))
    cid = collection.find("QA", SRC)["id"]
    kids = collection.get(cid)["child_ids"]
    s = _summary(cid)                                             # parses: never a half-written file
    assert sorted(fake.calls) == sorted(kids) and len(set(fake.calls)) == 16   # each run once
    assert fake.max_active <= 4
    assert all(s["children"][k]["status"] == "done" and s["children"][k]["finished_at"] for k in kids)
    assert not (collection.folder(cid) / "test_summary.tmp").exists()


# ---------------------------------------------------------------- parent: shared vision

def test_shared_vision_is_computed_once_and_copied_into_every_run(uploads, monkeypatch):
    h = _harness()
    fake = _install(h, monkeypatch, FakeSpawn())
    h.run(_args(children=3))
    cid = collection.find("QA", SRC)["id"]
    kids = collection.get(cid)["child_ids"]
    shared = collection.folder(cid) / "shared_features" / SLUG
    for v in VIDEOS:
        ref = (shared / f"{v}.json").read_bytes()
        for k in kids:
            assert (uploads / k / "runs" / v / "llm" / SLUG / "features.json").read_bytes() == ref
    assert not (uploads / SRC / "runs" / VIDEOS[0] / "llm").exists()    # never into the source
    assert _summary(cid)["shared_features"]["computed_in"] == kids[0]


# ---------------------------------------------------------------- worker

def _child_with_features(kids):
    for kid in kids:
        for v in VIDEOS:
            d = config.UPLOADS / kid / "runs" / v / "llm" / SLUG
            d.mkdir(parents=True, exist_ok=True)
            (d / "features.json").write_text(json.dumps({"hook_strength": "strong", "frames": []}))


def test_a_retried_run_scores_fresh_again_and_the_gate_reads_the_new_iteration(uploads, cli):
    h = _harness()
    c = collection.create(SRC, "Retry", children=1)
    kid = c["child_ids"][0]
    _child_with_features([kid])
    fake = cli(_scorer)
    r1 = h._run_phase(kid, "claude_code", MODEL, None, "shared")
    r2 = h._run_phase(kid, "claude_code", MODEL, None, "shared")    # e.g. the parent died before saving r1
    assert r1["status"] == r2["status"] == "done"
    assert len(fake.calls) == 2 * 2 * len(VIDEOS)                   # both attempts made every call
    assert (r1["iteration"], r2["iteration"]) == (1, 2) and r2["gate"]["iteration"] == 2


def test_a_usage_limit_fails_the_run_fast_and_is_flagged(uploads, cli):
    h = _harness()
    c = collection.create(SRC, "Limit", children=1)
    kid = c["child_ids"][0]
    _child_with_features([kid])
    later = time.time() + 5 * 3600
    fake = cli(_events("Claude usage limit reached", is_error=True, limit={"status": "rejected", "resetsAt": later}))
    r = h._run_phase(kid, "claude_code", MODEL, None, "shared")
    assert r["status"] == "error" and r["rate_limited"] is True
    backoff = [x for x in fake.sleeps if x >= 30]                # (the 0.5 s entries are the harness's own job poll)
    assert len(fake.calls) == len(VIDEOS) and backoff == []       # one call per video, no backoff, P5 never tried
    assert "RateLimited" in r["error"]


def test_a_cli_error_on_one_video_marks_the_run_failed_not_done(uploads, cli):
    h = _harness()
    c = collection.create(SRC, "OneBad", children=1)
    kid = c["child_ids"][0]
    _child_with_features([kid])
    state = {"n": 0}

    def reply(stdin):
        state["n"] += 1
        return _events("Something broke", is_error=True, status=400) if state["n"] == 1 else _scorer(stdin)
    cli(reply)
    r = h._run_phase(kid, "claude_code", MODEL, None, "shared")
    assert r["status"] == "error" and r["rate_limited"] is False and "LLMError" in r["error"]


def test_the_worker_never_writes_the_source_and_only_evaluate_reads_answer_keys(uploads, cli, monkeypatch):
    h = _harness()
    c = collection.create(SRC, "ReadOnly", children=1)
    kid = c["child_ids"][0]
    _child_with_features([kid])
    before = _digest(uploads / SRC)
    readers = []
    real_read_csv = pd.read_csv

    def spy(path, *a, **kw):
        if Path(str(path)).name.startswith("ground_truth"):
            import traceback
            readers.append([f.filename for f in traceback.extract_stack()[:-1]])
        return real_read_csv(path, *a, **kw)
    monkeypatch.setattr(pd, "read_csv", spy)
    cli(_scorer)
    r = h._run_phase(kid, "claude_code", MODEL, None, "shared")
    assert r["status"] == "done"
    assert _digest(uploads / SRC) == before
    assert readers, "the comparison must come from the answer key"
    for stack in readers:                       # the frame right below read_csv is app/evaluate.py
        assert Path(stack[-1]).name == "evaluate.py", stack[-1]
    script = (Path(h.__file__)).read_text(encoding="utf-8")
    assert "ground_truth" not in script


def test_worker_result_line_survives_a_process_boundary(uploads, cli, capsys):
    """worker() prints exactly one MARK line that _spawn can parse, also when the phase raises."""
    h = _harness()
    args = SimpleNamespace(phase="run", worker="20990101-000000-000000", provider="claude_code", model=MODEL,
                           prompt=None, vision="shared")
    h.worker(args)                                                 # unknown session -> ValueError
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith(h.MARK)]
    assert len(lines) == 1
    out = json.loads(lines[0][len(h.MARK):])
    assert out["status"] == "error" and "ValueError" in out["error"] and out["rate_limited"] is False


# ---------------------------------------------------------------- report

def test_report_with_no_finished_run_renders_without_crashing():
    h = _harness()
    data = {"collection": "col-x", "name": "Empty", "source": SRC, "vision": "shared",
            "llm": {"provider": "claude_code", "model": MODEL},
            "children": {"a": {"status": "running", "index": 1}, "b": {"status": "error", "index": 2, "error": "x"}}}
    r = h.build_report(data)
    assert r["runs"] == {"planned": 2, "done": 0, "error": 1, "running_or_interrupted": 1}
    assert r["accuracy"]["p6"]["mean"] is None and r["gate_pass_rate"] is None
    text = h.render(r)
    assert "n/a" in text and "failures:" in text


def test_report_math_on_known_runs():
    h = _harness()

    def run(acc6, fp6, acc5, fp5, gate, finals, p5s, lat):
        return {"status": "done", "elapsed_s": 100.0,
                "videos": {f"v{i}": {"final": a, "p5_final": b, "model_verdict": a, "score": 50, "p5_score": 40}
                           for i, (a, b) in enumerate(zip(finals, p5s))},
                "comparison": {"basis": "cac_outcome", "beats_baseline": gate, "why": "",
                               "p6": {"accuracy": acc6, "fp": fp6, "tp": 1, "winners": 2},
                               "p5": {"accuracy": acc5, "fp": fp5, "tp": 2, "winners": 2}},
                "gate": {"beats_baseline": gate},
                "calls": [{"kind": "p6", "latency_s": lat, "cost_usd": 0.1}, {"kind": "p5", "latency_s": lat, "cost_usd": 0.1}]}
    data = {"name": "M", "llm": {"provider": "claude_code", "model": MODEL}, "vision": "shared", "children": {
        "a": run(0.5, 0, 0.75, 1, True, ["SCALE", "DO_NOT_SCALE"], ["SCALE", "SCALE"], 10),
        "b": run(1.0, 2, 0.25, 1, False, ["DEFER_TO_HUMAN", "SCALE"], ["DO_NOT_SCALE", "SCALE"], 20),
        "c": {"status": "error", "error": "x"}}}
    r = h.build_report(data)
    assert r["accuracy"]["p6"] == {"n": 2, "mean": 0.75, "stdev": 0.354, "min": 0.5, "max": 1.0}
    assert r["false_scale"]["p6"]["mean"] == 1.0 and r["false_scale"]["p6_runs_with_any"] == 1
    assert r["false_scale"]["p5_runs_with_any"] == 2
    assert r["gate_pass_rate"] == 0.5
    # a: exact 1/2, scale-call 1/2 ; b: exact 1/2, scale-call (no,no),(yes,yes) = 2/2
    assert r["agreement_p5_p6"]["exact_verdict"]["mean"] == 0.5
    assert r["agreement_p5_p6"]["scale_call"]["mean"] == 0.75
    assert r["cost_usd_list_price"] == {"total": 0.4, "per_run": 0.2, "note": r["cost_usd_list_price"]["note"]}
    assert r["per_video"]["v0"]["p6"] == {"SCALE": 1, "DEFER_TO_HUMAN": 1} and r["per_video"]["v0"]["p6_modal_share"] == 0.5
    assert r["runs"]["error"] == 1 and len(r["failures"]) == 1


def test_a_usage_limit_without_reset_info_does_not_sleep_through_the_whole_run(uploads, cli):
    h = _harness()
    c = collection.create(SRC, "LimitNoReset", children=1)
    kid = c["child_ids"][0]
    _child_with_features([kid])
    epoch = int(time.time() + 5 * 3600)
    fake = cli(_events(f"Claude AI usage limit reached|{epoch}", is_error=True))
    r = h._run_phase(kid, "claude_code", MODEL, None, "shared")
    assert r["status"] == "error" and r["rate_limited"] is True
    backoff = sum(x for x in fake.sleeps if x >= 30)
    assert backoff <= config.CLAUDE_CODE_MAX_WAIT_S, f"{backoff:.0f} s of backoff, {len(fake.calls)} CLI calls"
