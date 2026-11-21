"""Tests for bias_detection_final.py. Free: no API calls.

    python test_bias_detection.py      # plain runner (no pytest needed)
    python -m pytest -q test_bias_detection.py
"""

import math

import pandas as pd

import bias_detection_final as b


def frame(rows, condition="t"):
    """rows: (group, y_true, y_pred, score)."""
    return pd.DataFrame([{"condition": condition, "g": g, "y_true": t, "y_pred": p, "score": s}
                         for g, t, p, s in rows])


def test_metrics_hand_computed():
    # group a: 3/4 selected, group b: 1/4 selected
    df = frame([("a", 1, 1, 80), ("a", 1, 1, 80), ("a", 0, 1, 70), ("a", 0, 0, 10),
                ("b", 1, 1, 80), ("b", 1, 0, 40), ("b", 0, 0, 10), ("b", 0, 0, 10)])
    m = b.fairness(df, "g", perm=False)
    assert m["dp_diff"] == 0.5
    assert math.isclose(m["di_ratio"], 1 / 3)
    assert m["tpr_gap"] == 0.5           # TPR a = 1.0, b = 0.5
    assert m["fpr_gap"] == 0.5           # FPR a = 0.5, b = 0.0
    assert m["accuracy"] == 0.75
    assert m["score_gap"] == 25.0        # 60 vs 35


def test_perfectly_fair():
    df = frame([(g, t, t, 50) for g in "ab" for t in (0, 1)])
    m = b.fairness(df, "g")
    assert (m["dp_diff"], m["di_ratio"], m["tpr_gap"], m["fpr_gap"]) == (0, 1, 0, 0)
    assert m["score_gap_p"] > 0.5


def test_edge_nobody_selected():
    # disparate impact is undefined (0/0), must be NaN not a crash or a fake 1.0
    df = frame([(g, 0, 0, 10) for g in "ab"])
    m = b.fairness(df, "g", perm=False)
    assert m["dp_diff"] == 0 and math.isnan(m["di_ratio"])


def test_edge_group_without_positives():
    # group b has no qualified rows -> TPR gap cannot be computed across 2 groups
    df = frame([("a", 1, 1, 80), ("a", 0, 0, 10), ("b", 0, 0, 10), ("b", 0, 1, 70)])
    m = b.fairness(df, "g", perm=False)
    assert math.isnan(m["tpr_gap"]) and m["fpr_gap"] == 0.5   # FPR a = 0, b = 1/2


def test_permutation_detects_real_gap():
    df = frame([("a", 1, 1, 90)] * 20 + [("b", 1, 1, 50)] * 20)
    assert b.permutation_p(df, "g", n=500) < 0.01


def test_equalize_thresholds_removes_rate_gap():
    rows = [("a", 1, 1, s) for s in (90, 85, 80, 75)] + [("b", 1, int(s >= 60), s) for s in (70, 55, 50, 45)]
    df = frame(rows)
    assert b.fairness(df, "g", perm=False)["dp_diff"] > 0
    post = b.equalize_thresholds(df, "g")
    assert post["condition"].iloc[0] == "post_g"
    assert b.fairness(post, "g", perm=False)["dp_diff"] == 0
    assert df["y_pred"].tolist() == [1, 1, 1, 1, 1, 0, 0, 0]   # input not mutated


def test_count_words_whole_words_only():
    assert b.count_words("Strong fit, strongly aligned. STRONG!", ["strong"]) == 2
    assert b.count_words("", ["strong"]) == 0


def test_mock_pipeline_detects_and_blind_removes_planted_bias():
    base, blind = b.run("baseline", "mock"), b.run("blind", "mock")
    assert len(base) == len(b.RESUMES) * len(b.NAMES)
    assert b.fairness(base, "race", perm=False)["di_ratio"] < 0.8      # planted bias found
    assert b.fairness(blind, "race", perm=False)["dp_diff"] == 0       # blinding removes it
    assert b.name_effect(blind)["max_score_spread"] == 0


def test_fair_prompt_differs_and_baseline_prompt_unchanged():
    assert b.FAIR_SYSTEM.startswith(b.SYSTEM) and "TODO" not in b.FAIR_SYSTEM
    # SYSTEM must stay identical to the original template so the cache is re-used
    src = (b.HERE / "bias_detection.py").read_text()
    assert b.SYSTEM.split("\n", 1)[1] in src      # instructions part of the template
    assert f'JOB = "{b.JOB}"' in src


def test_offline_mode_never_calls_api():
    try:
        b.ask(b.SYSTEM, "Applicant: Nobody\nRésumé: not in cache " + "x" * 10, "offline")
    except b.NotCached:
        return
    raise AssertionError("offline mode must raise NotCached for unknown prompts")


def test_cached_baseline_complete_if_present():
    """If the real cache exists, every baseline and blind prompt must be found in it."""
    if not b.CACHE.exists() or not any(b.CACHE.iterdir()):
        return
    for cond in ("baseline", "blind"):
        assert len(b.run(cond, "offline")) == 64


if __name__ == "__main__":
    tests = [f for n, f in sorted(globals().items()) if n.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print(f"{len(tests)} passed")
