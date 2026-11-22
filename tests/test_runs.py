"""Offline tests for runs (app/runs.py): the first Start runs in place and records its prompt/provider/model; Start on a
completed run creates a clean child; an unfinished run is resumed; views follow the run. No model, key or network."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app import collection, config, dataset, ingest, jobs, llm, pipeline, prompts, runs, scoring
from app import prompt_versions as pv
from app.main import app
from tests.test_claude_code import SRC, VIDEOS, uploads  # noqa: F401

REPLY = "VERDICT: SCALE\nSCORE: 70\nCONFIDENCE: MEDIUM\nREASONS:\n1. a 1%\n2. b 2%\n3. c 3%\n"


@pytest.fixture
def env(uploads, monkeypatch):                                        # noqa: F811
    calls = {"vision": 0, "score": 0}

    def analyse(vid, row, pre):
        calls["vision"] += 1
        return {"frames": [], "hook_strength": "medium"}

    def complete(prompt, **kw):
        calls["score"] += 1
        if calls.get("fail") and "VID=v2" in prompt:
            raise RuntimeError("provider down")
        return {"text": REPLY}
    monkeypatch.setattr(pipeline.vision, "analyse", analyse)
    monkeypatch.setattr(scoring.llm, "complete", complete)
    monkeypatch.setattr(llm, "usage", lambda r: {"provider": "anthropic", "model": config.MODEL})
    dataset._activate(SRC)
    return calls


def _wait():
    for _ in range(300):
        if not jobs.running():
            return
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def _sj(sid):
    return json.loads((config.UPLOADS / sid / "session.json").read_text())


def _v2():
    return pv.create("P6", prompts.P6.replace("--- End of examples ---", "TUNED\n--- End of examples ---"))["id"]


def test_first_start_runs_in_place_and_records_the_run(env):
    c = TestClient(app)
    p = c.get("/api/pipeline/plan").json()
    assert p["action"] == "first" and p["next_n"] == 1 and "first run" in p["message"] and p["run"] is None
    r = c.post("/api/pipeline/start", json={}).json()
    _wait()
    assert r["session_id"] == SRC and r["created_child"] is False
    run = _sj(SRC)["run"]
    assert (run["n"], run["prompt_id"], run["status"], run["scored"], run["videos"], run["errors"]) == (1, "P6", "done", 3, 3, 0)
    assert run["provider"] == config.llm_choice()["provider"] and run["prompt_sha256"] == pv.get("P6")["sha256"]
    assert "collection" not in _sj(SRC) and env["vision"] == 3


def test_start_on_a_completed_run_creates_a_clean_child_and_runs_there(env):
    c = TestClient(app)
    pid = _v2()
    c.post("/api/pipeline/start", json={}); _wait()
    gt = (config.UPLOADS / SRC / "ground_truth.csv").read_bytes()
    _ = dataset._load_session(config.UPLOADS / SRC)
    s = _sj(SRC)
    s["manual_review"] = {"Ana": {"submitted_at": "t", "rows": {"v1": {"decision": "SCALE"}}}}
    (config.UPLOADS / SRC / "session.json").write_text(json.dumps(s))
    (config.UPLOADS / SRC / "runs" / "decisions.jsonl").write_text('{"x": 1}\n')
    plan = c.get(f"/api/pipeline/plan?prompt_id={pid}").json()
    assert plan["action"] == "child" and plan["next_n"] == 2 and plan["vision_reuse"] is True
    assert "run 002" in plan["message"] and "only scoring makes model calls" in plan["message"]
    vision_before = env["vision"]
    r = c.post("/api/pipeline/start", json={"prompt_id": pid}).json()
    _wait()
    child = r["session_id"]
    assert r["created_child"] is True and child != SRC and config.DATASET_ID == child      # the child is now the open session
    cs, ps = _sj(child), _sj(SRC)
    assert ps["collection"]["index"] == 1 and cs["collection"] == {"id": ps["collection"]["id"], "name": "XTB · 1", "index": 2}
    assert cs["name"] == "XTB · 1 · run 002" and cs["run"]["prompt_id"] == pid and cs["run"]["n"] == 2
    assert cs["run"]["status"] == "done" and cs["run"]["vision_reused"] is True and env["vision"] == vision_before   # features reused
    assert ps["run"]["prompt_id"] == "P6" and ps["run"]["n"] == 1                                   # the parent keeps its own run
    assert cs.get("manual_review") == {} and "prompts" not in cs                                    # empty reviews, no adoption state
    assert not (config.UPLOADS / child / "runs" / "decisions.jsonl").exists()
    assert (config.UPLOADS / child / "ground_truth.csv").read_bytes() == gt                         # locked ground truth copied
    assert cs["ground_truth"]["locked_at"] == ps["ground_truth"]["locked_at"]
    assert not list((config.UPLOADS / child / "runs").glob("*/llm/*/score_*.json")) or \
        all(pipeline.load(v, pv.stage(pid)) for v in VIDEOS)                                       # only this run's own scores
    assert pipeline.load("v1", "score_p6") is None                                                  # P6 answers were not copied
    assert _sj(SRC)["manual_review"]["Ana"]["submitted_at"] == "t"                                  # the source is untouched


def test_a_different_model_reruns_vision_and_the_repeat_is_noted(env, monkeypatch):
    c = TestClient(app)
    c.post("/api/pipeline/start", json={}); _wait()
    plan = c.get("/api/pipeline/plan").json()
    assert plan["action"] == "child" and plan["repeat_of"] == [1] and "a repeat run" in plan["message"]
    s = _sj(SRC)
    s["llm"] = {"provider": "ollama", "model": "qwen2.5vl:7b"}
    (config.UPLOADS / SRC / "session.json").write_text(json.dumps(s))
    dataset._activate(SRC)
    plan = c.get("/api/pipeline/plan").json()
    assert plan["vision_reuse"] is False and plan["repeat_of"] == [] and "Vision is re-run" in plan["message"]
    before = env["vision"]
    r = c.post("/api/pipeline/start", json={}).json(); _wait()
    assert env["vision"] == before + 3 and _sj(r["session_id"])["run"]["vision_reused"] is False
    assert _sj(r["session_id"])["run"]["provider"] == "ollama"
    assert _sj(SRC)["llm"] == {"provider": _sj(SRC)["run"]["provider"], "model": _sj(SRC)["run"]["model"]}   # the source goes back to its run's model


def test_an_unfinished_run_resumes_in_place_or_asks(env):
    c = TestClient(app)
    pid = _v2()
    env["fail"] = True
    c.post("/api/pipeline/start", json={}); _wait()
    run = _sj(SRC)["run"]
    assert run["status"] == "partial" and (run["scored"], run["errors"]) == (2, 1)
    p = c.get("/api/pipeline/plan").json()
    assert p["action"] == "resume" and "Resume run 001" in p["button"] and "2 of 3" in p["message"]
    env["fail"] = False
    r = c.post("/api/pipeline/start", json={}).json(); _wait()
    assert r["created_child"] is False and _sj(SRC)["run"]["status"] == "done" and _sj(SRC)["run"]["scored"] == 3
    # unfinished + another prompt: ask; auto is refused, resume keeps the run's prompt, child leaves it alone
    env["fail"] = True
    s = _sj(SRC); s.pop("run"); (config.UPLOADS / SRC / "session.json").write_text(json.dumps(s))
    for v in VIDEOS:
        for f in (config.UPLOADS / SRC / "runs" / v / "llm").glob("*/score_*.json"):
            f.unlink()
    c.post("/api/pipeline/start", json={}); _wait()
    ask = c.get(f"/api/pipeline/plan?prompt_id={pid}").json()
    assert ask["action"] == "ask" and "Resume it with P6" in ask["message_resume"] and "run 002" in ask["message_child"]
    assert c.post("/api/pipeline/start", json={"prompt_id": pid}).status_code == 409
    env["fail"] = False
    r = c.post("/api/pipeline/start", json={"prompt_id": pid, "mode": "resume"}).json(); _wait()
    assert r["created_child"] is False and _sj(SRC)["run"]["prompt_id"] == "P6" and _sj(SRC)["run"]["status"] == "done"


def test_a_start_that_cannot_begin_leaves_no_child_and_no_parent(env, monkeypatch):
    c = TestClient(app)
    c.post("/api/pipeline/start", json={}); _wait()
    sessions = lambda: sorted(p.name for p in config.UPLOADS.iterdir() if not p.name.startswith("_"))
    before = sessions()
    monkeypatch.setattr(jobs, "start", lambda *a, **k: False)
    assert c.post("/api/pipeline/start", json={}).status_code == 409
    assert sessions() == before and config.DATASET_ID == SRC
    assert "collection" not in _sj(SRC) and not list((config.UPLOADS / "_collections").glob("*/collection.json"))
    assert c.post("/api/pipeline/start", json={"mode": "bogus"}).status_code == 422


def test_old_sessions_are_inferred_on_read_and_never_rewritten(env):
    c = TestClient(app)
    assert c.get("/api/pipeline/plan").json()["action"] == "first"
    for v in VIDEOS:
        pipeline.run_prompt(v, "P6")
    before = (config.UPLOADS / SRC / "session.json").read_bytes()
    p = c.get("/api/pipeline/plan").json()
    assert p["action"] == "child" and p["run"]["inferred"] is True and p["run"]["prompt_id"] == "P6"
    assert (config.UPLOADS / SRC / "session.json").read_bytes() == before and "run" not in _sj(SRC)
    assert runs.view_prompt() == "P6"


def test_views_and_the_dashboard_follow_each_runs_own_prompt(env):
    c = TestClient(app)
    pid = _v2()
    c.post("/api/pipeline/start", json={}); _wait()
    r = c.post("/api/pipeline/start", json={"prompt_id": pid}).json(); _wait()
    child = r["session_id"]
    vids = c.get("/api/videos").json()                                                  # open session = the child, no ?prompt=
    assert all(v["scored"] and v["prompt_id"] == pid for v in vids)
    assert c.get("/api/evaluate").json()["prompt"] == pid
    cid = _sj(child)["collection"]["id"]
    ev = collection.evaluations(cid)
    by = {x["index"]: x for x in ev["runs"]}
    assert [x["index"] for x in ev["runs"]] == [1, 2] and ev["failed"] == 0
    assert by[1]["run"]["prompt_id"] == "P6" and by[2]["run"]["prompt_id"] == pid and by[2]["run"]["provider"]
    assert "P6 multimodal, after rule guard" in by[1]["verdict_accuracy"]
    assert f"{pid} multimodal, after rule guard" in by[2]["verdict_accuracy"]          # labelled by the prompt that made them
    assert config.DATASET_ID == child                                                    # evaluation restored the open session
    assert collection.get(cid)["children"] == 2 and collection.next_index(cid) == 3


def test_start_is_refused_while_a_job_runs(env, monkeypatch):
    c = TestClient(app)
    monkeypatch.setattr(jobs, "running", lambda: True)
    assert c.post("/api/pipeline/start", json={}).status_code == 409


# ---------------------------------------------------------------- QA probes

def _tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_plan_endpoint_writes_nothing_and_calls_no_model(env, monkeypatch):
    c = TestClient(app)
    c.post("/api/pipeline/start", json={}); _wait()
    before = _tree(config.UPLOADS)
    names = sorted(p.name for p in config.UPLOADS.iterdir())
    monkeypatch.setattr(scoring.llm, "complete", lambda *a, **k: pytest.fail("plan must not call a model"))
    monkeypatch.setattr(pipeline.vision, "analyse", lambda *a, **k: pytest.fail("plan must not call vision"))
    for q in ("", "?prompt_id=P6", "?prompt_id="):
        assert c.get("/api/pipeline/plan" + q).status_code == 200
    for bad in ("P5", "../x", "p6.2", "P6.99"):
        assert c.get("/api/pipeline/plan", params={"prompt_id": bad}).status_code in (400, 404, 422), bad
    assert _tree(config.UPLOADS) == before and sorted(p.name for p in config.UPLOADS.iterdir()) == names


def test_child_keeps_locked_ground_truth_bytes_and_hash_and_refuses_edits(env):
    c = TestClient(app)
    c.post("/api/pipeline/start", json={}); _wait()
    src_gt = {p.name: p.read_bytes() for p in (config.UPLOADS / SRC).glob("ground_truth*")}
    r = c.post("/api/pipeline/start", json={}).json(); _wait()
    child = r["session_id"]
    assert child != SRC
    assert {p.name: p.read_bytes() for p in (config.UPLOADS / child).glob("ground_truth*")} == src_gt
    assert _sj(child)["ground_truth"] == _sj(SRC)["ground_truth"] or _sj(child)["ground_truth"].get("locked_at")
    assert not (config.UPLOADS / child / "runs" / "iterations").exists()
    assert _sj(child).get("manual_review") == {}


def test_failed_start_in_an_existing_collection_deletes_only_the_new_child(env, monkeypatch):
    c = TestClient(app)
    c.post("/api/pipeline/start", json={}); _wait()
    c.post("/api/pipeline/start", json={}); _wait()                          # now SRC=run 001 and a child run 002 exist
    cid = _sj(SRC)["collection"]["id"]
    kids = collection.get(cid)["child_ids"]
    dataset._activate(SRC)
    snap = _tree(config.UPLOADS)
    orig = jobs.start
    monkeypatch.setattr(jobs, "start", lambda *a, **k: False)
    r = c.post("/api/pipeline/start", json={})
    assert r.status_code == 409
    after = _tree(config.UPLOADS)
    kept = {k: v for k, v in snap.items() if not k.startswith("_collections")}
    assert all(after.get(k) == v for k, v in kept.items() if k.split("/")[0] in {SRC, *kids} and not k.endswith("session.json"))
    assert collection.get(cid)["child_ids"] == kids and (config.UPLOADS / SRC).is_dir() and all((config.UPLOADS / k).is_dir() for k in kids)
    assert sorted(p.name for p in config.UPLOADS.iterdir() if p.name[0].isdigit()) == sorted({SRC, *kids})


def test_a_copy_that_fails_halfway_leaves_no_orphan_session(env, monkeypatch):
    c = TestClient(app)
    c.post("/api/pipeline/start", json={}); _wait()
    before = {p.name for p in config.UPLOADS.iterdir()}
    monkeypatch.setattr(dataset, "_strip_for_run", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        runs.start()
    assert {p.name for p in config.UPLOADS.iterdir() if p.name[0].isdigit()} == {n for n in before if n[0].isdigit()}
    assert "collection" not in _sj(SRC) and not (config.UPLOADS / "_collections").exists() or not list((config.UPLOADS / "_collections").iterdir())


def test_a_failed_copy_does_not_leave_an_empty_auto_parent_or_marker(env, monkeypatch):
    c = TestClient(app)
    c.post("/api/pipeline/start", json={}); _wait()
    monkeypatch.setattr(dataset, "_strip_for_run", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        runs.start()
    assert "collection" not in _sj(SRC)
    d = config.UPLOADS / "_collections"
    assert not d.exists() or not [p for p in d.iterdir()]


def test_double_start_after_completion_makes_exactly_one_child(env):
    import threading
    c = TestClient(app)
    c.post("/api/pipeline/start", json={}); _wait()
    out = []
    def go():
        try:
            out.append(c.post("/api/pipeline/start", json={}).status_code)
        except Exception as e:                     # an unhandled server error
            out.append(f"{type(e).__name__}: {e}")
    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]; _wait()
    kids = [p for p in config.UPLOADS.iterdir() if p.name[0].isdigit()]
    assert len(kids) == 2, (out, [p.name for p in kids])                      # source + ONE child
    assert sorted(map(str, out)) == ["200", "409"], out


def test_stale_running_status_after_a_restart_is_resumable(env):
    c = TestClient(app)
    s = _sj(SRC)
    s["run"] = {"n": 1, "prompt_id": "P6", "provider": config.llm_choice()["provider"], "model": config.llm_choice()["model"],
                "status": "running", "scored": 1, "videos": 3, "errors": 0}
    (config.UPLOADS / SRC / "session.json").write_text(json.dumps(s))
    p = c.get("/api/pipeline/plan").json()
    assert p["action"] == "resume"
    r = c.post("/api/pipeline/start", json={}); _wait()
    assert r.status_code == 200 and _sj(SRC)["run"]["status"] == "done" and "collection" not in _sj(SRC)


def test_a_provider_error_makes_the_run_partial_and_the_next_start_resumes_in_place(env):
    c = TestClient(app)
    env["fail"] = True
    c.post("/api/pipeline/start", json={}); _wait()
    run = _sj(SRC)["run"]
    assert run["status"] == "partial" and run["errors"] == 1 and run["scored"] == 2
    env["fail"] = False
    assert c.get("/api/pipeline/plan").json()["action"] == "resume"
    r = c.post("/api/pipeline/start", json={}).json(); _wait()
    assert r["created_child"] is False and _sj(SRC)["run"]["status"] == "done"


def test_run_numbers_do_not_collide_after_a_middle_child_is_deleted(env):
    c = TestClient(app)
    c.post("/api/pipeline/start", json={}); _wait()
    ids = []
    for _ in range(3):
        r = c.post("/api/pipeline/start", json={}).json(); _wait()
        ids.append(r["session_id"])
        dataset._activate(SRC)
    dataset.delete_session(ids[1])
    r = c.post("/api/pipeline/start", json={}).json(); _wait()
    ns = [(_sj(p.name).get("collection") or {}).get("index") for p in config.UPLOADS.iterdir() if p.name[0].isdigit()]
    assert len(ns) == len(set(ns)), ns


@pytest.mark.parametrize("bad", ["../x", "P6.2/..", "%2e%2e", "p6"])
def test_start_with_bad_prompt_ids_is_refused_and_creates_nothing(env, bad):
    c = TestClient(app)
    c.post("/api/pipeline/start", json={}); _wait()
    before = sorted(p.name for p in config.UPLOADS.iterdir())
    r = c.post("/api/pipeline/start", json={"prompt_id": bad})
    assert r.status_code in (400, 404, 422)
    assert sorted(p.name for p in config.UPLOADS.iterdir()) == before


def test_collection_evaluation_and_reviews_count_run_001_once(env):
    c = TestClient(app)
    c.post("/api/pipeline/start", json={}); _wait()
    c.post("/api/pipeline/start", json={}); _wait()
    cid = _sj(SRC)["collection"]["id"]
    ev = c.get(f"/api/collections/{cid}/evaluation").json()
    ids = [r["id"] for r in ev["runs"]]
    assert len(ids) == len(set(ids)) == 2 and SRC in ids
    rv = c.get(f"/api/collections/{cid}/reviews").json()
    assert len([r["id"] for r in rv["runs"]]) == 2


def test_a_failed_start_restores_the_sources_model_and_run_record(env, monkeypatch):
    c = TestClient(app)
    c.post("/api/pipeline/start", json={}); _wait()
    s = _sj(SRC)
    s["llm"] = {"provider": "ollama", "model": "qwen2.5vl:7b"}                 # the "next run" choice, differs from the run's model
    (config.UPLOADS / SRC / "session.json").write_text(json.dumps(s))
    dataset._activate(SRC)
    run_before = _sj(SRC)["run"]
    monkeypatch.setattr(jobs, "start", lambda *a, **k: False)
    assert c.post("/api/pipeline/start", json={}).status_code == 409
    assert _sj(SRC)["llm"] == {"provider": "ollama", "model": "qwen2.5vl:7b"}   # not reset to the run's model by the failed start
    assert _sj(SRC)["run"] == run_before
    # an in-place start that cannot begin leaves no "running" record either
    s = _sj(SRC); s.pop("run"); (config.UPLOADS / SRC / "session.json").write_text(json.dumps(s))
    assert c.post("/api/pipeline/start", json={}).status_code == 409
    assert "run" not in _sj(SRC)


def test_run_numbers_are_never_reused(env, monkeypatch):
    c = TestClient(app)
    c.post("/api/pipeline/start", json={}); _wait()
    r = c.post("/api/pipeline/start", json={}).json(); _wait()
    cid = _sj(SRC)["collection"]["id"]
    assert _sj(r["session_id"])["collection"]["index"] == 2 and collection.next_index(cid) == 3
    dataset._activate(SRC)
    collection.remove_run(cid, r["session_id"])                                     # run 002 is gone ...
    assert collection.next_index(cid) == 3                                          # ... and 002 is not handed out again
    r2 = c.post("/api/pipeline/start", json={}).json(); _wait()
    assert _sj(r2["session_id"])["collection"]["index"] == 3
    rec = collection._read(cid)
    rec.pop("next_index"); collection._write(rec)                                   # an older record without the field
    assert collection.next_index(cid) == 4


def test_session_writes_use_unique_temp_files(env, monkeypatch):
    names = []
    real = type(config.UPLOADS / "x").replace
    monkeypatch.setattr(type(config.UPLOADS), "replace", lambda self, target: (names.append(self.name), real(self, target))[1])
    runs._patch(SRC, lambda s: s.__setitem__("x", 1))
    runs._patch(SRC, lambda s: s.__setitem__("x", 2))
    assert len(names) == 2 and names[0] != names[1] and all(n.startswith("session.json.") and n.endswith(".tmp") for n in names)
