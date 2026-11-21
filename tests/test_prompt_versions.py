"""Prompt versions, iterations and the adoption gate (app/prompt_versions.py, app/iterations.py).

Offline: the LLM, media and summary steps are fakes, so these tests check the loop itself - versions
are immutable, every iteration scores the version next to P5, results are frozen, and a version is
adopted only when it beats P5 against the ground truth (or with a logged, written override).
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app import config, ingest, iterations, jobs, llm, pipeline, prompts, scoring
from app import prompt_versions as pv
from app.main import app

SID = "20260101-000000-000000"
ROW = {"hours_since_post": 120, "reach": 40000, "views": 52000, "total_interactions": 3900, "saved": 600, "shares": 300}
WINNERS = {"v1", "v3"}                       # the ground truth: v1, v3 hit target CAC; v2, v4 did not


def _reply(verdict):
    return f"VERDICT: {verdict}\nSCORE: 70\nCONFIDENCE: MEDIUM\nREASONS:\n1. a 1%\n2. b 2%\n3. c 3%\n"


def fake_complete(prompt, **kw):
    """P5 never scales. A prompt with TUNED scales exactly the winners; one with RECKLESS scales everything."""
    vid = prompt.rsplit("VID=", 1)[1].split()[0]
    if "RECKLESS" in prompt:
        v = "SCALE"
    elif "TUNED" in prompt:
        v = "SCALE" if vid in WINNERS else "DO_NOT_SCALE"
    else:
        v = "DO_NOT_SCALE"
    fake_complete.calls += 1
    return {"text": _reply(v)}


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    for k in ("UPLOADS", "DATASET_ID", "DATA", "MEDIA", "RUNS", "DECISIONS_LOG"):
        monkeypatch.setattr(config, k, getattr(config, k))
    monkeypatch.setattr(config, "UPLOADS", tmp_path)
    (tmp_path / SID).mkdir()
    (tmp_path / SID / "session.json").write_text(json.dumps({"id": SID, "name": "x", "form": {}}))
    config.activate(SID)
    videos = {v: dict(ROW, video_id=v) for v in ("v1", "v2", "v3", "v4")}
    monkeypatch.setattr(ingest, "load_inputs", lambda: (videos, {}))
    monkeypatch.setattr(pipeline, "fetch_clip", lambda vid: False)
    monkeypatch.setattr(pipeline.media, "preprocess", lambda vid, row: {"frames": []})
    monkeypatch.setattr(pipeline.vision, "analyse", lambda vid, row, pre: {})
    monkeypatch.setattr(pipeline, "summary", lambda vid, multimodal: f"VID={vid} ")
    monkeypatch.setattr(pipeline, "card", lambda vid, prompt_id=None: {"video_id": vid})
    fake_complete.calls = 0
    monkeypatch.setattr(scoring.llm, "complete", fake_complete)
    monkeypatch.setattr(llm, "usage", lambda r: {"provider": "anthropic", "model": config.MODEL})
    jobs.reset()
    return tmp_path / SID


def _truth(folder, outcomes=True):
    rows = ["video_id,expected_verdict,performed_well_after_scaling,cac"]
    for v in ("v1", "v2", "v3", "v4"):
        win = v in WINNERS
        rows.append(f"{v},{'SCALE' if win else 'DO_NOT_SCALE'},{('yes' if win else 'no') if outcomes else ''},"
                    f"{(20 if win else 60) if outcomes else ''}")
    (folder / "ground_truth.csv").write_text("\n".join(rows) + "\n")


def _variant(marker):
    return prompts.P6.replace("--- End of examples ---", f"{marker}\n--- End of examples ---")


def _wait():
    for _ in range(200):
        if not jobs.running():
            return
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def _iterate(client, pid):
    r = client.post("/api/iterations", json={"prompt_id": pid})
    assert r.status_code == 200, r.text
    _wait()
    return client.get("/api/iterations").json()[0]


# ---------------------------------------------------------------- versions

@pytest.mark.parametrize("text, msg", [
    ("", "empty"),
    ("no placeholder here", "exactly once"),
    ("{video_summary} and {video_summary}", "exactly once"),
    ("{video_summary} {brand}", "{brand}"),
    ("{video_summary} {", "unbalanced"),
])
def test_invalid_templates_are_refused(text, msg):
    with pytest.raises(ValueError, match=msg):
        pv.validate(text)


def test_builtin_prompts_validate_and_p5_is_unchanged():
    pv.validate(prompts.P5)
    pv.validate(prompts.P6)
    assert prompts.P5 == (config.ROOT / "app" / "p5_template.txt").read_text(encoding="utf-8")


def test_versions_are_immutable_numbered_and_never_reuse_an_id(campaign):
    a = pv.create("P6", _variant("TUNED"), "cite timestamps")
    assert (a["id"], a["parent"], a["sha256"]) == ("P6.2", "P6", pv.sha256(_variant("TUNED")))
    with pytest.raises(ValueError, match="identical to P6.2"):
        pv.create("P6", _variant("TUNED"))
    with pytest.raises(ValueError, match="identical to P6"):
        pv.create("P6", prompts.P6)
    with pytest.raises(ValueError, match="fixed baseline"):
        pv.create("P5", _variant("X"))
    b = pv.create("P6.2", _variant("TUNED2"))
    assert b["parent"] == "P6.2"
    pv.delete("P6.3")
    assert pv.create("P6", _variant("TUNED3"))["id"] == "P6.4"
    ids = [v["id"] for v in pv.listing()["versions"]]
    assert ids == ["P5", "P6", "P6.2", "P6.4"] and pv.active() == "P6"


def test_builtins_and_the_active_version_cannot_be_deleted(campaign):
    with pytest.raises(ValueError):
        pv.delete("P6")
    pv.create("P6", _variant("TUNED"))
    pv.set_active("P6.2", override_reason="testing the delete guard")
    with pytest.raises(pv.Conflict):
        pv.delete("P6.2")


def test_each_version_has_its_own_cache_and_a_changed_text_is_rescored(campaign):
    pv.create("P6", _variant("TUNED"))
    a = pipeline.run_prompt("v1", "P6")
    b = pipeline.run_prompt("v1", "P6.2")
    assert (a["verdict"], b["verdict"]) == ("DO_NOT_SCALE", "SCALE")
    assert b["prompt_id"] == "P6.2" and b["prompt_sha256"] == pv.get("P6.2")["sha256"]
    folder = next((campaign / "runs" / "v1" / "llm").iterdir())
    assert {p.name for p in folder.iterdir()} == {"score_p6.json", "score_p6.2.json"}
    calls = fake_complete.calls
    pipeline.run_prompt("v1", "P6.2")
    assert fake_complete.calls == calls                   # cached
    p = folder / "score_p6.2.json"
    p.write_text(json.dumps(dict(json.loads(p.read_text()), prompt_sha256="stale")))
    pipeline.run_prompt("v1", "P6.2")
    assert fake_complete.calls == calls + 1               # a different prompt text is never reused


def test_diff_shows_the_change_against_the_parent(campaign):
    pv.create("P6", _variant("TUNED"))
    d = TestClient(app).get("/api/prompts/P6.2/diff").text
    assert "--- P6" in d and "+++ P6.2" in d and "+TUNED" in d


# ---------------------------------------------------------------- iterations and the gate

def test_a_better_version_beats_p5_and_is_adopted(campaign):
    _truth(campaign)
    c = TestClient(app)
    assert c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED"), "note": "n"}).status_code == 201
    r = c.put("/api/prompts/active", json={"id": "P6.2"})
    assert r.status_code == 409 and "no finished iteration" in r.json()["detail"]

    it = _iterate(c, "P6.2")
    assert it["status"] == "done" and it["n"] == 1 and it["prompt_id"] == "P6.2" and it["errors"] == 0
    assert it["videos"]["v1"] == {**it["videos"]["v1"], "final": "SCALE", "p5_final": "DO_NOT_SCALE"}
    cmp_ = it["comparison"]
    assert cmp_["basis"] == "cac_outcome" and cmp_["beats_baseline"] is True
    assert cmp_["candidate"]["cac_outcome"]["accuracy"] == 1.0 and cmp_["baseline"]["cac_outcome"]["accuracy"] == 0.5

    r = c.put("/api/prompts/active", json={"id": "P6.2"})
    assert r.status_code == 200 and r.json()["active"] == "P6.2"
    log = r.json()["adoptions"][-1]
    assert log["beats_baseline"] is True and log["iteration"] == 1 and log["override_reason"] is None
    assert pipeline.active_stage() == "score_p6.2"


def test_a_version_with_more_false_scale_is_refused_unless_overridden_in_writing(campaign):
    _truth(campaign)
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("RECKLESS")})
    it = _iterate(c, "P6.2")
    assert it["comparison"]["beats_baseline"] is False and "more false SCALE" in it["comparison"]["why"]
    assert c.put("/api/prompts/active", json={"id": "P6.2"}).status_code == 409
    assert c.put("/api/prompts/active", json={"id": "P6.2", "override_reason": "short"}).status_code == 409
    r = c.put("/api/prompts/active", json={"id": "P6.2", "override_reason": "brand asked for reach this month"})
    assert r.status_code == 200
    log = r.json()["adoptions"][-1]
    assert log["beats_baseline"] is False and log["override_reason"] == "brand asked for reach this month"
    # P6 can always be restored; P5 is the baseline, never a candidate
    assert c.put("/api/prompts/active", json={"id": "P6"}).json()["active"] == "P6"
    assert c.put("/api/prompts/active", json={"id": "P5"}).status_code == 400


def test_without_ground_truth_an_iteration_is_frozen_but_not_judged(campaign):
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    it = _iterate(c, "P6.2")
    assert it["status"] == "done" and it["comparison"]["beats_baseline"] is None
    assert "ground truth" in it["comparison"]["why"]
    assert c.put("/api/prompts/active", json={"id": "P6.2"}).status_code == 409


def test_gate_falls_back_to_expected_verdict_without_cac_outcomes(campaign):
    _truth(campaign, outcomes=False)
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    cmp_ = _iterate(c, "P6.2")["comparison"]
    assert cmp_["basis"] == "expected_scale" and cmp_["beats_baseline"] is True


def test_iteration_on_another_model_does_not_count_for_this_one(campaign, monkeypatch):
    _truth(campaign)
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    _iterate(c, "P6.2")
    monkeypatch.setattr(config, "llm_choice", lambda: {"provider": "ollama", "model": "qwen2.5vl:7b"})
    assert iterations.adoption_check("P6.2")["beats_baseline"] is False


def test_iteration_refuses_p5_unknown_ids_and_a_second_job(campaign, monkeypatch):
    c = TestClient(app)
    assert c.post("/api/iterations", json={"prompt_id": "P5"}).status_code == 400
    assert c.post("/api/iterations", json={"prompt_id": "P6.9"}).status_code == 404
    monkeypatch.setattr(jobs, "start", lambda *a, **k: False)
    assert c.post("/api/iterations", json={"prompt_id": "P6"}).status_code == 409
    assert c.get("/api/iterations").json() == []          # a refused start leaves no record


def test_review_card_follows_the_adopted_version(campaign):
    _truth(campaign)
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    _iterate(c, "P6.2")
    c.put("/api/prompts/active", json={"id": "P6.2"})
    listed = {v["video_id"]: v["verdict"] for v in c.get("/api/videos").json()}
    assert listed == {"v1": "SCALE", "v2": "DO_NOT_SCALE", "v3": "SCALE", "v4": "DO_NOT_SCALE"}


# ---------------------------------------------------------------- choosing the prompt of a run

def test_a_run_with_a_chosen_version_uses_its_own_cache_and_does_not_adopt_it(campaign):
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    assert c.post("/api/pipeline/start", json={"prompt_id": "P6.2"}).status_code == 200
    _wait()
    assert pv.active() == "P6" and c.get("/api/prompts").json()["adoptions"] == []     # choosing is not adopting
    s = pipeline.load("v1", "score_p6.2")
    assert s["prompt_id"] == "P6.2" and s["verdict"] == "SCALE" and "rule_hits" in s    # which version, guard applied
    assert pipeline.load("v1", "score_p6") is None                                     # no collision with P6
    run = json.loads((campaign / "session.json").read_text())["run"]                   # the run is tied to its prompt
    assert (run["prompt_id"], run["status"], run["scored"], run["videos"]) == ("P6.2", "done", 4, 4)
    calls = fake_complete.calls
    assert pipeline.run_prompt("v1", "P6.2")["verdict"] == "SCALE" and fake_complete.calls == calls          # cached per version
    pipeline.run_prompt("v1", "P6.2", force=True)
    assert fake_complete.calls == calls + 1                                            # force recomputes that version only


def test_a_run_refuses_unknown_p5_and_unusable_prompt_ids(campaign, monkeypatch):
    c = TestClient(app)
    assert c.post("/api/pipeline/start", json={"prompt_id": "P6.9"}).status_code == 422
    assert c.post("/api/pipeline/start", json={"prompt_id": "P5"}).status_code == 422
    assert c.post("/api/videos/v1/process?prompt=nope").status_code == 422
    assert c.get("/api/videos/v1/card?prompt=nope").status_code == 422
    assert not jobs.running() and pipeline.load("v1", "score_p6") is None               # nothing ran


def test_process_and_card_follow_the_chosen_prompt(campaign, monkeypatch):
    seen = []
    monkeypatch.setattr(pipeline, "card", lambda vid, prompt_id=None: seen.append(prompt_id) or {"video_id": vid})
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    assert c.post("/api/videos/v1/process?prompt=P6.2").status_code == 200
    c.get("/api/videos/v1/card?prompt=P6.2"); c.get("/api/videos/v1/card")
    assert seen == ["P6.2", "P6.2", "P6.2"] and pipeline.load("v1", "score_p6.2")["verdict"] == "SCALE"   # no ?prompt= -> the run's (inferred)
    assert pv.active() == "P6"


# ---------------------------------------------------------------- QA probes: choosing a prompt for a run

@pytest.mark.parametrize("bad", ["../x", "..%2F..%2Fetc", "p6.2", "P6.2 ", "P6.02", "P6.2/../P6", "P6.", "score_p6", "P6.2.json", "%00"])
def test_run_prompt_ids_are_matched_exactly_and_never_touch_the_filesystem(campaign, bad):
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    before = sorted(p.relative_to(campaign.parent) for p in campaign.parent.rglob("*"))
    for r in (c.post("/api/pipeline/start", json={"prompt_id": bad}), c.post("/api/videos/v1/process", params={"prompt": bad}),
              c.get("/api/videos/v1/card", params={"prompt": bad})):
        assert r.status_code == 422, (bad, r.status_code, r.text)
    assert sorted(p.relative_to(campaign.parent) for p in campaign.parent.rglob("*")) == before
    assert not jobs.running() and fake_complete.calls == 0


def test_empty_prompt_means_active_and_card_of_a_version_never_run_is_blank(campaign, monkeypatch):
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    assert c.post("/api/videos/v1/process", params={"prompt": ""}).status_code == 200
    assert pipeline.load("v1", "score_p6") is not None and pipeline.load("v1", "score_p6.2") is None


def test_card_of_a_never_run_version_shows_no_answer_not_the_active_one(campaign, monkeypatch):
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    pipeline.run_prompt("v1", "P6")                                 # only the active P6 has an answer
    monkeypatch.setattr(pipeline, "card", lambda vid, prompt_id=None: {
        "score": pipeline.load(vid, pv.stage(prompt_id) if prompt_id else pipeline.active_stage()),
        "prompt_id": prompt_id or pv.active()})
    j = c.get("/api/videos/v1/card", params={"prompt": "P6.2"}).json()
    assert j["score"] is None and j["prompt_id"] == "P6.2"
    assert c.get("/api/videos/v1/card").json()["score"]["verdict"] == "DO_NOT_SCALE"


def test_deleted_version_is_refused_and_its_cache_removed(campaign):
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    pipeline.run_prompt("v1", "P6.2")
    assert pipeline.load("v1", "score_p6.2")
    assert c.delete("/api/prompts/P6.2").status_code == 200
    assert pipeline.load("v1", "score_p6.2") is None
    assert c.get("/api/videos/v1/card", params={"prompt": "P6.2"}).status_code == 422
    assert c.post("/api/pipeline/start", json={"prompt_id": "P6.2"}).status_code == 422


def test_deleting_a_version_mid_run_leaves_no_cached_answers(campaign, monkeypatch):
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    state = {"n": 0}

    def deleting(prompt, **kw):
        if state["n"] == 0:
            pv.delete("P6.2")
        state["n"] += 1
        return fake_complete(prompt, **kw)
    monkeypatch.setattr(scoring.llm, "complete", deleting)
    try:
        pipeline.run_prompt("v1", "P6.2")
    except KeyError:
        pass
    assert not list(config.RUNS.glob("*/llm/*/score_p6.2.json"))


def test_choosing_a_version_leaves_active_adoptions_and_ground_truth_alone(campaign):
    c = TestClient(app)
    _truth(campaign)
    gt = (campaign / "ground_truth.csv").read_bytes()
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    sess = json.loads((campaign / "session.json").read_text()).get("prompts")
    c.post("/api/pipeline/start", json={"prompt_id": "P6.2", "force": True}); _wait()
    assert json.loads((campaign / "session.json").read_text()).get("prompts") == sess
    assert (campaign / "ground_truth.csv").read_bytes() == gt


def test_score_video_cli_bad_prompt_exits_before_any_scoring(campaign, monkeypatch, tmp_path):
    import importlib.util, sys
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("score_video", root / "scripts" / "score_video.py")
    sv = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(root))
    spec.loader.exec_module(sv)
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    for bad in ("P5", "P6.9", "../x", "p6.2"):
        monkeypatch.setattr(sys, "argv", ["score_video.py", SID, "--all", "--prompt", bad])
        with pytest.raises(SystemExit) as e:
            sv.main()
        assert e.value.code not in (0, None) and "unknown or unusable prompt" in str(e.value.code)
    assert fake_complete.calls == 0 and not (config.RUNS / "cli_results.json").exists()
    monkeypatch.setattr(sys, "argv", ["score_video.py", SID, "--all", "--prompt", "P6.2"])
    with pytest.raises(SystemExit) as e:
        sv.main()
    assert e.value.code == 0 and fake_complete.calls == 4 and pv.active() == "P6"
    out = json.loads((config.RUNS / "cli_results.json").read_text())
    assert {r["prompt_id"] for r in out} == {"P6.2"}


# ---------------------------------------------------------------- the global library

def _lib_file():
    return config.UPLOADS / "_prompts" / "library.json"


def _make_session(sid, versions, active=None, nxt=None, adoptions=None, name=None):
    d = config.UPLOADS / sid
    (d / "runs").mkdir(parents=True, exist_ok=True)
    st = {"versions": versions, "next": nxt or (len(versions) + 2), "adoptions": adoptions or []}
    if active:
        st["active"] = active
    (d / "session.json").write_text(json.dumps({"id": sid, "name": name or sid, "form": {}, "prompts": st}))
    return d


def _ver(pid, text, parent="P6"):
    return {"id": pid, "parent": parent, "note": "n", "created_at": "t", "sha256": pv.sha256(text), "text": text}


def test_versions_live_in_the_global_library_not_in_session_json(campaign):
    c = TestClient(app)
    r = c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED"), "note": "why"})
    assert r.status_code == 201 and r.json()["id"] == "P6.2"
    lib = json.loads(_lib_file().read_text())
    assert [v["id"] for v in lib["versions"]] == ["P6.2"] and lib["next"] == 3 and lib["migrated"] is True
    assert "prompts" not in json.loads((campaign / "session.json").read_text())
    assert not list(_lib_file().parent.glob("*.tmp"))                                  # atomic: no temp file left
    config.activate(None)                                                              # no campaign open: still readable
    j = c.get("/api/prompts").json()
    assert [v["id"] for v in j["versions"]] == ["P5", "P6", "P6.2"] and j["active"] is None and j["adoptions"] is None
    assert c.post("/api/prompts", json={"parent": "P6.2", "text": _variant("TUNED2")}).json()["id"] == "P6.3"


def test_any_session_can_run_a_library_version_and_ids_are_never_reused(campaign):
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    other = config.UPLOADS / "20260102-000000-000000"
    (other / "runs").mkdir(parents=True)
    (other / "session.json").write_text(json.dumps({"id": other.name, "name": "other", "form": {}}))
    config.activate(other.name)
    assert [v["id"] for v in c.get("/api/prompts").json()["versions"]] == ["P5", "P6", "P6.2"]
    assert c.get("/api/prompts").json()["active"] == "P6"                              # adoption is per session
    c.delete("/api/prompts/P6.2")
    assert c.post("/api/prompts", json={"parent": "P6", "text": _variant("AGAIN")}).json()["id"] == "P6.3"


def test_delete_is_refused_while_any_session_has_the_version_active(campaign):
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    other = config.UPLOADS / "20260102-000000-000000"
    (other / "runs").mkdir(parents=True)
    (other / "session.json").write_text(json.dumps({"id": other.name, "name": "Other", "form": {},
                                                    "prompts": {"active": "P6.2", "adoptions": []}}))
    r = c.delete("/api/prompts/P6.2")
    assert r.status_code == 409 and "Other" in r.json()["detail"]
    assert "P6.2" in [v["id"] for v in c.get("/api/prompts").json()["versions"]]


def test_delete_is_refused_while_a_job_runs(campaign, monkeypatch):
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    monkeypatch.setattr(jobs, "running", lambda: True)
    assert c.delete("/api/prompts/P6.2").status_code == 409
    assert "P6.2" in [v["id"] for v in c.get("/api/prompts").json()["versions"]]


def test_library_ids_are_allocated_under_a_lock(campaign):
    import threading
    ids, errs = [], []

    def make(i):
        try:
            ids.append(pv.create("P6", _variant(f"T{i}"))["id"])
        except Exception as e:                                                          # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=make, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs and len(set(ids)) == 8


def test_migration_reuses_same_text_keeps_free_ids_and_renumbers_clashes(tmp_path, monkeypatch):
    for k in ("UPLOADS", "DATASET_ID", "DATA", "MEDIA", "RUNS", "DECISIONS_LOG"):
        monkeypatch.setattr(config, k, getattr(config, k))
    monkeypatch.setattr(config, "UPLOADS", tmp_path)
    A, B, C = _variant("AAA"), _variant("BBB"), _variant("CCC")
    a = _make_session("20260101-000000-000001", [_ver("P6.2", A)], active="P6.2", name="a")
    b = _make_session("20260101-000000-000002", [_ver("P6.2", A), _ver("P6.3", B, "P6.2")], name="b")      # same text as a's
    c = _make_session("20260101-000000-000003", [_ver("P6.2", C)], active="P6.2",
                      adoptions=[{"id": "P6.2", "at": "t"}], name="c")                                  # clash: other text
    stale = c / "runs" / "v1" / "llm" / "anthropic--m"
    stale.mkdir(parents=True)
    (stale / "score_p6.2.json").write_text("{}")
    (c / "ground_truth.csv").write_text("video_id,expected_verdict\nv1,SCALE\n")
    gt = (c / "ground_truth.csv").read_bytes()
    lib = json.loads(json.dumps(pv._lib()))
    ids = {v["id"]: v["text"] for v in lib["versions"]}
    assert ids == {"P6.2": A, "P6.3": B, "P6.4": C}                                                     # A reused, C renumbered
    sess = lambda d: json.loads((d / "session.json").read_text()).get("prompts", {})
    assert "versions" not in sess(a) and "next" not in sess(b)
    assert sess(c)["active"] == "P6.4" and sess(c)["adoptions"][0]["id"] == "P6.4"                     # follows the new id
    assert not (stale / "score_p6.2.json").exists()                                                    # stale cache dropped
    assert (c / "ground_truth.csv").read_bytes() == gt                                                  # answer key untouched
    before = _lib_file().read_text()
    assert json.loads(json.dumps(pv._lib())) == json.loads(before) and _lib_file().read_text() == before   # idempotent
    assert pv.create("P6", _variant("DDD"))["id"] == "P6.5"                                             # next id is past every import


def test_copy_session_does_not_copy_versions(campaign):
    from app import dataset
    (campaign / "session.json").write_text(json.dumps({"id": SID, "name": "x", "form": {},
        "prompts": {"versions": [], "next": 5, "active": "P6", "adoptions": [{"id": "P6"}]}}))
    new = dataset.copy_session(SID)
    st = json.loads((config.UPLOADS / new["id"] / "session.json").read_text())["prompts"]
    assert "versions" not in st and "next" not in st and st["active"] == "P6" and st["adoptions"] == [{"id": "P6"}]


# ---------------------------------------------------------------- QA probes: global library + migration

def _qh(n):
    return pv.sha256(f"text {n} {{video_summary}}")


def _qver(pid, n, parent="P6"):
    return {"id": pid, "parent": parent, "note": f"n{n}", "created_at": "t", "sha256": _qh(n), "text": f"text {n} {{video_summary}}"}


@pytest.fixture
def qa_lib_env(tmp_path, monkeypatch):
    for k in ("UPLOADS", "DATASET_ID", "DATA", "MEDIA", "RUNS", "DECISIONS_LOG"):
        monkeypatch.setattr(config, k, getattr(config, k))
    monkeypatch.setattr(config, "UPLOADS", tmp_path)
    config.DATA = config.RUNS = None
    config.DATASET_ID = None
    return tmp_path


def _mk(root, sid, prompts=None, raw=None):
    d = root / sid
    (d / "runs").mkdir(parents=True)
    rec = {"id": sid, "name": sid, "form": {}}
    if prompts is not None:
        rec["prompts"] = prompts
    (d / "session.json").write_text(raw if raw is not None else json.dumps(rec))
    return d


def _sj(d):
    return json.loads((d / "session.json").read_text())


def _three(root):
    a = _mk(root, "20260101-000000-000001", {"versions": [_qver("P6.2", 1), _qver("P6.3", 2, "P6.2")], "next": 4, "active": "P6.3",
                                              "adoptions": [{"id": "P6.3", "at": "t"}]})
    b = _mk(root, "20260101-000000-000002", {"versions": [_qver("P6.2", 9), _qver("P6.3", 1)], "next": 4, "active": "P6.2",
                                              "adoptions": [{"id": "P6.2", "at": "t"}]})
    c = _mk(root, "20260101-000000-000003")                       # no prompts key at all
    return a, b, c


def test_migration_remaps_ids_active_and_adoptions_and_is_idempotent(qa_lib_env):
    a, b, c = _three(qa_lib_env)
    lib = pv._lib()
    ids = {v["id"]: v["sha256"] for v in lib["versions"]}
    assert ids == {"P6.2": _qh(1), "P6.3": _qh(2), "P6.4": _qh(9)}            # same text reuses an id; the clash got a fresh one
    assert len(ids) == len(lib["versions"]) and lib["next"] >= 5
    assert _sj(a)["prompts"] == {"active": "P6.3", "adoptions": [{"id": "P6.3", "at": "t"}]}
    assert _sj(b)["prompts"] == {"active": "P6.4", "adoptions": [{"id": "P6.4", "at": "t"}]}
    assert "prompts" not in _sj(c)
    snap = ((qa_lib_env / "_prompts" / "library.json").read_bytes(), _sj(a), _sj(b))
    pv._lib()
    assert ((qa_lib_env / "_prompts" / "library.json").read_bytes(), _sj(a), _sj(b)) == snap
    assert pv.create("P6", "brand new {video_summary}")["id"] == "P6.5"


def test_migration_drops_stale_cache_of_a_clashing_id_only(qa_lib_env):
    a, b, _ = _three(qa_lib_env)
    for d, st in ((a, "score_p6.2"), (b, "score_p6.2"), (b, "score_p6.3")):
        f = d / "runs" / "v1" / "llm" / "anthropic--m" / f"{st}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}")
    pv._lib()
    assert (a / "runs/v1/llm/anthropic--m/score_p6.2.json").exists()       # a kept its id: cache still valid
    assert not (b / "runs/v1/llm/anthropic--m/score_p6.2.json").exists()   # b's P6.2 text was different: dropped


def test_migration_does_not_leave_a_cache_under_an_id_that_now_means_another_text(qa_lib_env):
    _mk(qa_lib_env, "20260101-000000-000001", {"versions": [_qver("P6.2", 1), _qver("P6.3", 2)], "next": 4})
    b = _mk(qa_lib_env, "20260101-000000-000002", {"versions": [_qver("P6.2", 2)], "next": 3})     # b's P6.2 text == library P6.3
    f = b / "runs/v1/llm/anthropic--m/score_p6.2.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"prompt_sha256": _qh(2)}))
    assert pv.get("P6.2")["sha256"] == _qh(1)                                # in the library P6.2 is text 1
    assert not f.exists()


def test_crash_mid_migration_loses_no_versions(qa_lib_env, monkeypatch):
    a, b, _ = _three(qa_lib_env)
    real = pv._write_json
    n = {"k": 0}

    def flaky(p, rec):
        if p.name == "session.json":
            n["k"] += 1
            if n["k"] == 2:
                raise OSError("disk full")
        return real(p, rec)
    monkeypatch.setattr(pv, "_write_json", flaky)
    with pytest.raises(OSError):
        pv._lib()
    monkeypatch.setattr(pv, "_write_json", real)
    pv._lib()
    texts = {v["sha256"] for v in pv._lib()["versions"]}
    assert {_qh(1), _qh(2), _qh(9)} <= texts


@pytest.mark.parametrize("raw", ['[1,2]', json.dumps({"id": "x", "prompts": {"versions": [{"id": "P6.2", "text": "t"}], "next": 3}})])
def test_a_bad_session_file_does_not_break_the_whole_library(qa_lib_env, raw):
    _mk(qa_lib_env, "20260101-000000-000001", raw=raw)
    _mk(qa_lib_env, "20260101-000000-000002", {"versions": [_qver("P6.2", 1)], "next": 3})
    # a version entry without sha256 is still importable (the hash is recomputed from its text); a non-object file is skipped
    assert _qh(1) in {v["sha256"] for v in pv._lib()["versions"]}


def test_corrupt_session_json_is_skipped_by_migration(qa_lib_env):
    _mk(qa_lib_env, "20260101-000000-000001", raw="{nope")
    _mk(qa_lib_env, "20260101-000000-000002", {"versions": [_qver("P6.2", 1)], "next": 3})
    assert [v["id"] for v in pv._lib()["versions"]] == ["P6.2"]


def test_library_missing_empty_and_corrupt(qa_lib_env):
    assert pv._lib()["versions"] == [] and (qa_lib_env / "_prompts" / "library.json").exists()
    (qa_lib_env / "_prompts" / "library.json").write_text("{bad")
    with pytest.raises(ValueError, match="not valid JSON"):
        pv._lib()


def test_ids_are_never_reused_after_deleting_the_highest(qa_lib_env):
    assert pv.create("P6", "one {video_summary}")["id"] == "P6.2"
    assert pv.create("P6", "two {video_summary}")["id"] == "P6.3"
    pv.delete("P6.3")
    assert pv.create("P6", "three {video_summary}")["id"] == "P6.4"


def test_delete_refused_when_active_in_any_saved_session_without_leaking_paths(qa_lib_env):
    pv.create("P6", "one {video_summary}")
    _mk(qa_lib_env, "20260101-000000-000001", {"active": "P6.2", "adoptions": []})
    with pytest.raises(pv.Conflict) as e:
        pv.delete("P6.2")
    assert str(qa_lib_env) not in str(e.value) and "20260101-000000-000001" in str(e.value) or "20260101" in str(e.value)
    assert str(qa_lib_env) not in str(e.value)


def test_delete_refused_while_a_job_runs(qa_lib_env, monkeypatch):
    pv.create("P6", "one {video_summary}")
    monkeypatch.setattr(jobs, "running", lambda: True)
    with pytest.raises(pv.Conflict):
        pv.delete("P6.2")


def test_collection_children_copy_has_no_versions_and_listing_without_session_is_null(qa_lib_env):
    pv.create("P6", "one {video_summary}")
    j = pv.listing()
    assert j["active"] is None and j["adoptions"] is None and any(v["id"] == "P6.2" for v in j["versions"])


def test_concurrent_creates_get_distinct_ids(qa_lib_env):
    import threading
    out = []
    ts = [threading.Thread(target=lambda i=i: out.append(pv.create("P6", f"t{i} {{video_summary}}")["id"])) for i in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(set(out)) == 12


def test_malformed_version_entries_are_skipped_and_the_rest_imported(qa_lib_env):
    _mk(qa_lib_env, "20260101-000000-000001", {"versions": [{"id": "P6.2"}, "junk", {"id": "bad", "text": "x"},
                                                             _qver("P6.3", 1)], "next": 4})
    assert [v["sha256"] for v in pv._lib()["versions"]] == [_qh(1)]



def test_video_list_defaults_to_the_runs_prompt_and_prompt_param_overrides(campaign):
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    c.post("/api/pipeline/start", json={"prompt_id": "P6.2"}); _wait()
    assert pipeline.load("v1", "score_p6") is None                                # only score_p6.2 exists
    default = {v["video_id"]: v for v in c.get("/api/videos").json()}             # tied to the run, no selector needed
    assert all(v["scored"] for v in default.values()) and default["v1"]["verdict"] == "SCALE"
    assert default["v1"]["prompt_id"] == "P6.2" and default["v1"]["active_prompt"] == "P6"
    other = c.get("/api/videos?prompt=P6").json()                                 # an explicit override still works
    assert not any(v["scored"] or v["verdict"] for v in other) and other[0]["prompt_id"] == "P6"
    assert c.get("/api/videos?prompt=P6.9").status_code == 422
    assert c.get("/api/videos?prompt=P5").status_code == 422
    assert pv.active() == "P6"


def test_evaluation_labels_the_version_that_produced_the_verdicts(campaign):
    from app import evaluate
    _truth(campaign)
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    c.post("/api/pipeline/start", json={"prompt_id": "P6.2"}); _wait()
    d = c.get("/api/evaluate?prompt=P6").json()                                   # explicit: the active prompt, which has no answers
    assert d["prompt"] == "P6" and d["prompt_info"] == {"id": "P6", "scored": 0, "videos": 4, "other_text": 0, "active": "P6"}
    assert c.get("/api/evaluate").json()["prompt"] == "P6.2"                      # default: the run's own prompt
    e = c.get("/api/evaluate?prompt=P6.2").json()
    assert e["prompt"] == "P6.2" and e["prompt_info"]["scored"] == 4 and e["prompt_info"]["active"] == "P6"
    assert e["llm"] == config.llm_choice()                                        # provider / model of those answers
    assert {v["video_id"]: v["p6_final"] for v in e["videos"]}["v1"] == "SCALE"   # the P6.2 verdicts, not P6's
    assert e["classification"]["expected_verdict"]["all"]["P6 multimodal"]["tp"] == 2   # API keys stay "P6 ..."; the UI relabels
    assert c.get("/api/evaluate?prompt=P6.9").status_code == 422 and c.get("/api/evaluate?prompt=P5").status_code == 422
    assert pv.active() == "P6" and evaluate.run(run_missing=False)["prompt"] == "P6"      # evaluate.run itself still defaults to active


def test_evaluation_reports_answers_made_from_another_text(campaign):
    from app import evaluate
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    c.post("/api/pipeline/start", json={"prompt_id": "P6.2"}); _wait()
    s = pipeline.load("v1", "score_p6.2")
    pipeline._save("v1", "score_p6.2", dict(s, prompt_sha256="0" * 64))
    info = evaluate.run(run_missing=False, prompt_id="P6.2")["prompt_info"]
    assert (info["scored"], info["other_text"]) == (4, 1)                          # honest about the mismatch, not one clean label


def test_evaluate_never_calls_a_model_unless_run_missing_and_keys_are_unchanged(campaign, monkeypatch):
    _truth(campaign)
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    monkeypatch.setattr(scoring.llm, "complete", lambda *a, **k: pytest.fail("evaluation without run_missing must not call a model"))
    for q in ("", "?prompt=P6.2", "?prompt=", "?prompt=P6&reviewer=nobody"):
        r = c.get("/api/evaluate" + q)
        assert r.status_code == 200, q
    j = c.get("/api/evaluate?prompt=P6.2").json()
    assert list(j["verdict_accuracy"]) == ["P5 text-only, model verdict", "P5 text-only, after rule guard",
                                           "P6 multimodal, model verdict", "P6 multimodal, after rule guard"]
    assert j["prompt_info"]["scored"] == 0                          # nothing scored, nothing invented
    assert all(v["p6_final"] is None for v in j["videos"])          # blank stays None, not 0 or the active prompt's answer
    for bad in ("P5", "../x", "p6.2", "P6.99"):
        assert c.get("/api/evaluate", params={"prompt": bad}).status_code == 422, bad
    assert fake_complete.calls == 0


def test_evaluate_run_missing_scores_only_the_chosen_version_and_p5(campaign):
    c = TestClient(app)
    c.post("/api/prompts", json={"parent": "P6", "text": _variant("TUNED")})
    assert c.get("/api/evaluate?run_missing=true&prompt=P6.2").status_code == 200
    assert pipeline.load("v1", "score_p6.2") and pipeline.load("v1", "score_p5")
    assert pipeline.load("v1", "score_p6") is None and pv.active() == "P6"
