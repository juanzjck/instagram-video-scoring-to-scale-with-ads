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
    monkeypatch.setattr(pipeline, "card", lambda vid: {"video_id": vid})
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
