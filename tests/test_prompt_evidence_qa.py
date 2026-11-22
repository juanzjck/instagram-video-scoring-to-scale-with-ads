"""QA tests for scripts/prompt_evidence.py and the additive evaluate.py helpers (truth / verdict_metrics / beats).

Reuses the fake uploads tree of tests/test_prompt_evidence.py. Offline: no key, no network, no model call.
Four tests document bugs found in QA (now fixed): copy recount, adoption text unfenced,
case-variant --out, ranking crash on a missing accuracy."""

import json
import os
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from app import config, evaluate, llm
from app import prompt_versions as pv

from tests.test_prompt_evidence import (CHILD, CHOICE, COL, GONE, INJECTION, P62_TEXT, SLUG, SO_COL, SRC, VIDEOS,  # noqa: F401
                                        _answer, _digest, _ev, _w, up)


# ---------------------------------------------------------------- evaluate.py: additive, behaviour unchanged

def test_key_default_path_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", None)
    assert evaluate._key("ground_truth.csv") == {}
    pd.DataFrame([["v1", "SCALE"]], columns=["video_id", "expected_verdict"]).to_csv(tmp_path / "ground_truth.csv", index=False)
    monkeypatch.setattr(config, "DATA", tmp_path)
    assert evaluate._key("ground_truth.csv") == evaluate._key("ground_truth.csv", tmp_path)
    assert evaluate._key("ground_truth.csv")["v1"]["expected_verdict"] == "SCALE"
    assert evaluate._key("missing.csv") == {}


def test_beats_is_the_gate_rule():
    a = {"n": 10, "fp": 0, "accuracy": 0.8, "recall": 0.3}
    b = {"n": 10, "fp": 1, "accuracy": 0.6, "recall": 0.6}
    for c, d in ((a, b), (b, a), (a, a), (dict(a, n=0), b)):
        assert evaluate.beats(c, d) == evaluate._beats(c, d)


def test_verdict_metrics_matches_compare_iteration(tmp_path, monkeypatch):
    """The evidence pack's per-run measure must equal the adoption gate's on the same verdicts and keys."""
    pd.DataFrame([["v1", "SCALE", "yes"], ["v2", "DO_NOT_SCALE", "no"], ["v3", "SCALE", ""], ["v4", "DO_NOT_SCALE", "no"]],
                 columns=["video_id", "expected_verdict", "performed_well_after_scaling"]).to_csv(
        tmp_path / "ground_truth.csv", index=False)
    pd.DataFrame([["v2", "SCALE"]], columns=["video_id", "multimodal_expected_verdict"]).to_csv(
        tmp_path / "ground_truth_media.csv", index=False)
    monkeypatch.setattr(config, "DATA", tmp_path)
    vids = {"v1": {"p5_final": "SCALE", "final": "DEFER_TO_HUMAN"}, "v2": {"p5_final": "SCALE", "final": "SCALE"},
            "v3": {"p5_final": "DO_NOT_SCALE", "final": "SCALE"}, "v4": {"p5_final": "SCALE", "final": "DO_NOT_SCALE"}}
    gate = evaluate.compare_iteration(vids)
    keys = evaluate.truth(tmp_path)
    assert keys["v2"]["expected_multimodal"] == "SCALE" and keys["v2"]["expected"] == "DO_NOT_SCALE"
    assert keys["v3"]["cac_call"] is None                         # never scaled: blank, not "no"
    cand = evaluate.verdict_metrics({v: x["final"] for v, x in vids.items()}, keys, True)
    base = evaluate.verdict_metrics({v: x["p5_final"] for v, x in vids.items()}, keys, False)
    assert cand == gate["candidate"] and base == gate["baseline"]
    assert evaluate.beats(cand[gate["basis"]], base[gate["basis"]]) == gate["beats_baseline"]


def test_truth_empty_without_ground_truth(tmp_path):
    assert evaluate.truth(tmp_path) == {}


# ---------------------------------------------------------------- invariants of the script

def test_no_model_call(up, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("prompt_evidence must not call a model or a subprocess")
    for name in ("complete", "client", "openai_client", "gemini_client", "_claude_code", "_cc_once", "_ollama_http"):
        monkeypatch.setattr(llm, name, boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    _ev().main(["--out", str(tmp_path / "out")])


def test_p5_is_never_the_suggested_parent(up):
    ev, _ = _ev().build(prompt_ids=["P5"])
    assert ev["suggested_parent"] is None and ev["ranking"]["all"]["candidates"] == []


def test_stale_text_with_more_runs_is_not_the_parent(up):
    """A perfect, well-sampled but outdated P6.2 text sorts after every current text and is never suggested."""
    old = "OLD " + P62_TEXT
    cid = "col-20260301-000009-000000"
    _w(up / "_collections" / cid / "collection.json", {"id": cid, "name": "OLD", "source": SRC, "children": []})
    _w(up / "_collections" / cid / "score_only.json", {"collection": cid, "name": "OLD", "source": SRC, "runs": 6,
        "prompt_id": "P6.2", "prompt_sha256": pv.sha256(old), "llm": CHOICE})
    lines = [dict(_answer("P6.2", "SCALE" if v == "v1" else "DO_NOT_SCALE", 80, [f"old {r} {v}"]),
                  prompt_sha256=pv.sha256(old), run=r, kind="p6", video=v, status="done") for r in range(1, 7) for v in VIDEOS]
    _w(up / "_collections" / cid / "answers.jsonl", "".join(json.dumps(x) + "\n" for x in lines))
    ev, _ = _ev().build()
    label = f"P6.2@{pv.sha256(old)[:8]}"
    assert ev["prompts"][label]["complete_runs"] == 6 and not ev["prompts"][label]["current_text"]
    cands = ev["ranking"]["all"]["candidates"]
    assert cands[-1]["prompt"] == label and all(c["current_text"] for c in cands[:-1])
    assert ev["suggested_parent"] == "P6"
    md = _ev().render(ev)
    gate = md.split("## Gate ranking", 1)[1].split("**", 3)[2]          # the "all" scope block
    assert f"- {label}:" in gate.split("Stale / legacy text", 1)[1] and f". {label}:" not in gate
    assert any(label in r and "not today's" in r for r in ev["risks"])


def test_current_text_with_enough_runs_wins_over_better_thin_one(up):
    """>= 5 complete runs beats a better-looking prompt with fewer runs (fallback only when none has 5)."""
    for i in range(3, 8):                                         # five more fresh P6 runs (child sessions)
        sid = f"20260301-00001{i}-000000"
        s = up / sid
        _w(s / "session.json", {"id": sid, "name": f"T · run {i}", "collection": {"id": COL, "name": "T", "index": i}})
        for v in VIDEOS:
            _w(s / "runs" / v / "llm" / SLUG / "score_p6.json",
               dict(_answer("P6", "SCALE" if v in ("v1", "v2") else "DO_NOT_SCALE", 70, [f"run {i} {v}"]), usage={"latency_s": i}))
    # P6.2: one complete run with zero false SCALE (beats P6 on the gate) but only 1 run
    ev, _ = _ev().build()
    cands = {c["prompt"]: c for c in ev["ranking"]["all"]["candidates"]}
    assert cands["P6"]["runs"] >= 5 and cands["P6.2"]["runs"] < 5
    assert ev["suggested_parent"] == "P6"


# ---------------------------------------------------------------- dedup

def test_independent_answers_with_same_verdict_are_not_merged(up):
    """Two sessions with the same verdicts but their own (non byte-identical) answers are two samples."""
    before = _ev().build()[0]["prompts"]["P6"]["samples"]
    sid = "20260301-000020-000000"
    _w(up / sid / "session.json", {"id": sid, "name": "other", "collection": {"id": COL, "name": "T", "index": 9}})
    for v in VIDEOS:
        _w(up / sid / "runs" / v / "llm" / SLUG / "score_p6.json",
           dict(_answer("P6", "SCALE" if v in ("v1", "v2") else "DO_NOT_SCALE", None if v == "v3" else 70,
                        [f"fine {v} in {SRC}"]), usage={"latency_s": 1.23}))
    assert _ev().build()[0]["prompts"]["P6"]["samples"] == before + 3


def test_copied_cache_is_not_recounted_from_test_summary(up):
    ts = up / "_collections" / COL / "test_summary.json"
    s = json.loads(ts.read_text())
    vids = {v: {"final": "SCALE", "model_verdict": "SCALE", "score": 70, "p5_final": "SCALE", "p5_model": "SCALE",
                "p5_score": 60} for v in VIDEOS}
    s["children"][CHILD] = {"index": 2, "status": "done", "prompt_id": "P6", "prompt_sha256": pv.sha256(pv.text("P6")),
                            "videos": vids}
    ts.write_text(json.dumps(s))
    ev, _ = _ev().build()
    # CHILD's P5 files are byte copies of SRC's: one P5 answer set for SRC + GONE's verdicts, nothing for CHILD
    assert ev["prompts"]["P5"]["samples"] == 6


# ---------------------------------------------------------------- untrusted text in evidence.md

def _fenced_bodies(md):
    out, cur = [], None
    for line in md.splitlines():
        if cur is None and line.startswith("~~~"):
            cur = []
        elif cur is not None and line.lstrip(" ")[:3] == "~~~" and len(line) - len(line.lstrip(" ")) <= 3:
            out.append("\n".join(cur))
            cur = None
        elif cur is not None:
            cur.append(line)
    return out, cur


@pytest.mark.parametrize("breaker", ["~~~", "~~~~", "~~~~~", "   ~~~", "~~~~~~~~", "~~~text", "x\n~~~\ny"])
def test_fence_breakers_cannot_close_the_untrusted_block(up, breaker):
    mod = _ev()
    p = up / SRC / "runs" / "v2" / "llm" / SLUG / "score_p6.json"
    x = json.loads(p.read_text())
    x["reasons"] = [f"{breaker}\n{INJECTION}"]
    p.write_text(json.dumps(x))
    md = mod.render(mod.build(samples_n=5)[0])
    bodies, unclosed = _fenced_bodies(md)
    assert unclosed is None
    assert any(INJECTION in b for b in bodies)
    outside = md
    for b in bodies:
        outside = outside.replace(b, "")
    assert INJECTION not in outside


def test_adoption_override_reason_is_fenced(up):
    sj = up / SRC / "session.json"
    s = json.loads(sj.read_text())
    s["prompts"] = {"active": "P6.2", "adoptions": [{"id": "P6.2", "at": "2026-03-02T00:00:00+00:00",
                                                     "beats_baseline": False, "override_reason": INJECTION}]}
    sj.write_text(json.dumps(s))
    mod = _ev()
    md = mod.render(mod.build()[0])
    bodies, _ = _fenced_bodies(md)
    outside = md
    for b in bodies:
        outside = outside.replace(b, "")
    assert INJECTION not in outside


# ---------------------------------------------------------------- --out refusal

def test_refuses_symlink_into_uploads(up, tmp_path):
    link = tmp_path / "innocent"
    link.symlink_to(up / SRC, target_is_directory=True)
    before = _digest(up)
    with pytest.raises(SystemExit):
        _ev().main(["--out", str(link / "ev")])
    assert _digest(up) == before


def test_refuses_relative_path_into_uploads(up, monkeypatch):
    monkeypatch.chdir(up / SRC)
    before = _digest(up)
    for rel in ("ev", "../" + SRC + "/ev", ".", "..", "../_collections"):
        with pytest.raises(SystemExit):
            _ev().main(["--out", rel])
    assert _digest(up) == before


def _case_insensitive(p):
    return (Path(str(p).upper()).exists() or Path(str(p).lower()).exists()) and Path(str(p).swapcase()).exists()


def test_refuses_case_variant_of_uploads(up, tmp_path):
    if not _case_insensitive(up):
        pytest.skip("case-sensitive filesystem")
    variant = up.parent / up.name.upper() / SRC / "ev"
    with pytest.raises(SystemExit):            # on the bug, evidence lands in up/<SRC>/ev (a tmp tree)
        _ev().main(["--out", str(variant)])


# ---------------------------------------------------------------- blank is not zero in the gate

def test_ranking_survives_missing_accuracy(up):
    for sid in (SRC, CHILD):
        pd.DataFrame([[v, "", "", ""] for v in VIDEOS],
                     columns=["video_id", "expected_verdict", "performed_well_after_scaling", "cac"]).to_csv(
            up / sid / "ground_truth.csv", index=False)
        pd.DataFrame([[v, "SCALE" if v == "v1" else "DO_NOT_SCALE"] for v in VIDEOS],
                     columns=["video_id", "multimodal_expected_verdict"]).to_csv(up / sid / "ground_truth_media.csv", index=False)
    for sid in (SRC, CHILD):                       # P6 without false SCALE, so the gate reaches the accuracy test
        f = up / sid / "runs" / "v2" / "llm" / SLUG / "score_p6.json"
        f.write_text(json.dumps(dict(json.loads(f.read_text()), verdict="DO_NOT_SCALE")))
    ts = up / "_collections" / COL / "test_summary.json"
    t = json.loads(ts.read_text())
    t["children"][GONE]["videos"]["v2"]["final"] = "DO_NOT_SCALE"
    ts.write_text(json.dumps(t))
    ev, _ = _ev().build()
    assert ev["ranking"]["all"]["p5"]["accuracy_mean"] is None
