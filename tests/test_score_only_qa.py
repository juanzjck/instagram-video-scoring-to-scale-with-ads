"""QA gap tests for scripts/score_only.py (offline: the FakeCLI of test_claude_code.py, no key / network).
The last three (cut line, foreign collection, P5 coverage) are regression tests for bugs found in review."""

import json

import pytest

from app import collection, config, llm, pipeline
from tests.test_claude_code import _digest, _events, cli  # noqa: F401
from tests.test_score_only import (CHOICE, SRC, VIDEOS, _answers, _argv, _fake_collection, _scorer,  # noqa: F401
                                   _so, _text, _vid, src)


def _folder():
    return collection.folder(collection.find("SO", SRC)["id"])


# ---------------------------------------------------------------- choice: no global state, default unchanged

def test_choice_overrides_the_sessions_model_without_touching_global_state(src, cli):
    """The source session is on another provider: every call still goes to --provider, and neither
    config.llm_choice() nor session.json changes (no set_llm-style mutation)."""
    sj = src / SRC / "session.json"
    s = json.loads(sj.read_text())
    s["llm"] = {"provider": "ollama", "model": "llama-x"}
    sj.write_text(json.dumps(s))
    before = _digest(src / SRC)
    fake = cli(_scorer)
    _so().main(_argv(runs=2, concurrency=3))
    assert len(fake.calls) == 12
    assert all("--model" in c["cmd"] and c["cmd"][c["cmd"].index("--model") + 1] == CHOICE["model"]
               for c in fake.calls)
    assert config.resolve_llm(config._session_value("llm")) == {"provider": "ollama", "model": "llama-x"}
    assert _digest(src / SRC) == before
    assert {a["usage"]["provider"] for a in _answers(src)} == {"claude_code"}
    assert {a["usage"]["model"] for a in _answers(src)} == {CHOICE["model"]}


def test_choice_none_keeps_the_sessions_model(src, monkeypatch):
    """choice=None: llm.complete / pipeline.load / summary read the open session's model, as before."""
    config.activate(SRC)
    seen = {}

    def adapter(model, *a):
        seen["model"] = model
        return {"text": "ok", "input_tokens": 1, "output_tokens": 1, "stop_reason": "end_turn"}

    monkeypatch.setitem(llm._ADAPTERS, "claude_code", adapter)
    monkeypatch.setattr(llm, "_require", lambda p: None)
    assert llm.complete("hi")["model"] == CHOICE["model"] and seen["model"] == CHOICE["model"]
    other = {"provider": "claude_code", "model": "other-model"}
    assert llm.complete("hi", choice=other)["model"] == "other-model"
    assert config.llm_choice() == CHOICE
    # another model's features are read only when asked for
    d = src / SRC / "runs" / "v1" / "llm" / pipeline.llm_slug(other)
    d.mkdir(parents=True)
    (d / "features.json").write_text(json.dumps({"frames": [{"t_s": 0, "note": "OTHER-MODEL", "on_screen_text": None}]}))
    assert "feature-of-v1" in pipeline.summary("v1", True) and "OTHER-MODEL" not in pipeline.summary("v1", True)
    assert "OTHER-MODEL" in pipeline.summary("v1", True, choice=other)
    assert pipeline.load("v1", "features")["frames"][0]["note"] == "feature-of-v1"
    assert pipeline.load("v2", "features", other) is None


# ---------------------------------------------------------------- errors, refusals, blank-is-not-zero

def test_refusal_is_recorded_retried_and_does_not_stop_the_batch(src, cli):
    refusal = _events("I can't help with that.", stop="refusal")
    fake = cli(lambda stdin: refusal if _vid(_text(stdin)) == "v3" else _scorer(stdin))
    r = _so().main(_argv(runs=2, concurrency=1))
    assert len(fake.calls) == 12                                   # not a stop error
    errs = [a for a in _answers(src) if a["status"] == "error"]
    assert len(errs) == 4 and all("refusal" in a["error"] and a["rate_limited"] is False for a in errs)
    assert r["runs"]["done"] == 0 and r["runs"]["error"] == 2
    fake = cli(_scorer)
    r = _so().main(_argv(runs=2, concurrency=1))
    assert len(fake.calls) == 4 and r["runs"]["done"] == 2


def test_missing_score_stays_none_not_zero(src, cli):
    no_score = _events("VERDICT: DO_NOT_SCALE\nCONFIDENCE: LOW\nREASONS:\n1. a\n2. b\n3. c\n")
    cli(lambda stdin: no_score)
    r = _so().main(_argv(runs=2))
    assert all(a["score"] is None for a in _answers(src))
    assert r["per_video"]["v1"]["p6_score"] == {"n": 0, "mean": None, "stdev": None, "min": None, "max": None}
    assert r["per_video"]["v1"]["p5_score"]["mean"] is None
    assert "| 0 " not in (_folder() / "report.md").read_text()


def test_unparseable_answer_is_deferred_by_code(src, cli):
    cli(lambda stdin: _events("no verdict here"))
    _so().main(_argv(runs=1))
    a = _answers(src)
    assert all(x["verdict"] == "DEFER_TO_HUMAN" and x["model_verdict"] is None for x in a)


def test_provider_unavailable_stops_at_once(src, cli, monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_CODE_BIN", str(src / "no-such-claude"))
    r = _so().main(_argv(runs=5, concurrency=1))
    a = _answers(src)
    assert len(a) == 1 and a[0]["rate_limited"] is True and "ProviderUnavailable" in a[0]["error"]
    assert r["runs"]["done"] == 0


def test_rate_limit_with_concurrency_writes_only_whole_lines(src, cli):
    import time as _t
    n = {"i": 0}

    def scorer(stdin):
        n["i"] += 1
        if n["i"] > 5:
            return _events(is_error=True, status=429, result=f"Claude AI usage limit reached|{int(_t.time()) + 10 ** 6}")
        return _scorer(stdin)

    cli(scorer)
    _so().main(_argv(runs=10, concurrency=4))
    lines = (_folder() / "answers.jsonl").read_text().splitlines()
    assert all(json.loads(x) for x in lines)                       # every line is a whole record
    assert len(lines) < 60 and any(json.loads(x)["rate_limited"] for x in lines)


# ---------------------------------------------------------------- resume edge cases

def test_changed_prompt_or_source_input_is_refused_on_resume(src, cli):
    cli(_scorer)
    so = _so()
    so.main(_argv(runs=1))
    other = pipeline.llm_slug({"provider": "claude_code", "model": "claude-sonnet-5-5"})
    for v in VIDEOS:                                               # that model has features too
        d = src / SRC / "runs" / v / "llm"
        (d / other).mkdir()
        (d / other / "features.json").write_text((d / pipeline.llm_slug(CHOICE) / "features.json").read_text())
    with pytest.raises(SystemExit, match="another"):               # same --name, other model
        so.main(_argv("--model", "claude-sonnet-5-5", runs=1))
    vf = src / SRC / "videos.csv"
    vf.write_text(vf.read_text().replace("50000", "50001"))       # the source's data changed
    with pytest.raises(SystemExit, match="inputs"):
        so.main(_argv(runs=1))


def test_report_only_makes_no_call_and_appends_nothing(src, cli):
    so = _so()
    fake = cli(_scorer)
    so.main(_argv(runs=2))
    n, body = len(fake.calls), (_folder() / "answers.jsonl").read_text()
    r = so.main(_argv("--report-only", runs=5))                    # --runs does not grow it in report-only
    assert len(fake.calls) == n and (_folder() / "answers.jsonl").read_text() == body
    assert r["runs"]["planned"] == 2


def test_a_cut_last_line_does_not_swallow_the_next_record(src, cli):
    so = _so()
    cli(_scorer)
    so.main(_argv(runs=1))
    p = _folder() / "answers.jsonl"
    p.write_text(p.read_text() + '{"run": 2, "kind": "p6", "vid')   # crash mid-write
    so.main(_argv(runs=2))
    recs = so.read_answers(_folder())
    assert len([a for a in recs if a["run"] == 2]) == 6            # every run-2 answer readable


# ---------------------------------------------------------------- collections and P5 pairing

def test_name_of_a_run_collection_test_collection_is_refused(src, cli):
    cid = _fake_collection(src, 2)                                 # named "old", has test_summary.json
    folder = collection.folder(cid)
    (folder / "report.md").write_text("ORIGINAL REPORT")
    cli(_scorer)
    try:
        _so().main(_argv("--name", "old", runs=1))
    except SystemExit:
        pass
    assert (folder / "report.md").read_text() == "ORIGINAL REPORT"
    assert not (folder / "answers.jsonl").exists()


def test_p5_samples_missing_a_source_video_are_refused(src, cli):
    cid = _fake_collection(src, 2)
    p = collection.folder(cid) / "test_summary.json"
    d = json.loads(p.read_text())
    for k in d["children"].values():
        (k.get("videos") or {}).pop("v3", None)
    p.write_text(json.dumps(d))
    cli(_scorer)
    with pytest.raises(SystemExit, match="v3"):
        _so().main(_argv("--p5-from", cid, runs=2))


def test_p5_truncated_pairs_run_i_with_sample_i(src, cli):
    cid = _fake_collection(src, 4)                                 # sample i: v2/v3 SCALE when i even
    cli(_scorer)
    r = _so().main(_argv("--p5-from", cid, runs=3))
    assert r["per_video"]["v2"]["p5"] == {"DO_NOT_SCALE": 2, "SCALE": 1}
    assert r["score_only"]["p5"]["pairing"].startswith("truncated: the first 3 of 4")
    assert r["per_video"]["v2"]["p5_score"]["mean"] == 52.0        # p5_score 51, 52, 53
