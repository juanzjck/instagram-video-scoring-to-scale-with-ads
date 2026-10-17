"""Evaluate. The only module that opens the answer keys.

1. Verdicts: text-only P5 vs multimodal P6 against ground truth.
2. Content features: the vision output against ground_truth_media.csv.
3. Reviewer time: how long a human took to decide with the card vs with text only,
   read from the decision log.
"""

import json
from statistics import mean, median

import pandas as pd

from . import config, ingest, pipeline, scoring
from .ingest import num, val


def _key(name):
    p = config.DATA / name
    if not p.exists():
        return {}
    return {r["video_id"]: {k: val(v) for k, v in r.items()} for _, r in pd.read_csv(p, dtype=ingest.ID_DTYPES).iterrows()}


def _yn(b):
    return None if b is None else ("yes" if b else "no")


def _feature_checks(feats, gt):
    """Per-feature (got, expected, ok) for one video; None when either side is blank."""
    if not feats or not gt:
        return {}
    exp_first = num(gt.get("expected_product_first_seen_s"))
    got_first = num(feats.get("product_first_seen_s")) if feats.get("product_visible") else None
    issue_types = {i.get("type") for i in feats.get("safety_issues") or []}
    exp_issue = gt.get("expected_safety_issue") or "none"
    checks = {
        "hook_strength": (feats.get("hook_strength"), gt.get("expected_hook_strength")),
        "brand_match": (feats.get("brand_match"), gt.get("expected_brand_match")),
        "cta_present": (_yn(feats.get("cta_present")), gt.get("expected_cta_present")),
        "face_on_screen": (_yn(feats.get("face_on_screen")), gt.get("expected_face_on_screen")),
        "injection_in_media": (_yn("prompt_injection" in issue_types),
                               gt.get("expected_injection_in_media")),
        "safety_issue": (",".join(sorted(issue_types)) or "none", exp_issue),
    }
    out = {k: {"got": g, "expected": e, "ok": g == e} for k, (g, e) in checks.items()}
    out["safety_issue"]["ok"] = (exp_issue == "none" and not issue_types) or exp_issue in issue_types
    # Keyframes are sampled every 3s after the first 3s, so allow one sampling step.
    out["product_first_seen_s"] = {
        "got": got_first, "expected": exp_first,
        "ok": (got_first is None and exp_first is None)
              or (got_first is not None and exp_first is not None and abs(got_first - exp_first) <= 3),
    }
    return out


def read_decisions():
    if not config.DECISIONS_LOG.exists():
        return []
    return [json.loads(l) for l in config.DECISIONS_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]


def reviewer_timing():
    """Decision time by review mode, and how often the human agreed with the AI."""
    out = {}
    for mode in ("card", "text"):
        rows = [d for d in read_decisions() if d.get("mode") == mode and d.get("elapsed_ms") is not None]
        secs = [d["elapsed_ms"] / 1000 for d in rows]
        out[mode] = {
            "decisions": len(rows),
            "mean_s": round(mean(secs), 1) if secs else None,
            "median_s": round(median(secs), 1) if secs else None,
            "agreed_with_ai": sum(1 for d in rows if d.get("agrees_with_ai")),
        }
    return out


def run(run_missing=True):
    """Build the evaluation report. With run_missing, videos not yet scored are processed."""
    videos, _ = ingest.load_inputs()
    gt = _key("ground_truth.csv")
    gt_media = _key("ground_truth_media.csv")

    rows, feat_totals = [], {}
    for vid in videos:
        if run_missing:
            pipeline.process(vid)
            p5 = pipeline.run_p5(vid)
        else:
            p5 = pipeline.load(vid, "score_p5")
        p6 = pipeline.load(vid, "score_p6")
        feats = pipeline.load(vid, "features")

        expected = scoring.normalise_verdict((gt.get(vid) or {}).get("expected_verdict"))
        expected_mm = scoring.normalise_verdict(
            (gt_media.get(vid) or {}).get("multimodal_expected_verdict")) or expected
        checks = _feature_checks(feats, gt_media.get(vid))
        for k, c in checks.items():
            t = feat_totals.setdefault(k, {"correct": 0, "n": 0})
            t["n"] += 1
            t["correct"] += int(c["ok"])

        rows.append({
            "video_id": vid,
            "scenario": (gt.get(vid) or {}).get("scenario"),
            "expected": expected,
            "expected_multimodal": expected_mm,
            "p5_model": p5 and p5.get("model_verdict"),
            "p5_final": p5 and p5.get("verdict"),
            "p6_model": p6 and p6.get("model_verdict"),
            "p6_final": p6 and p6.get("verdict"),
            "p6_rule_override": bool(p6 and p6.get("rule_override")),
            "p6_reasons_citing_time": p6 and f"{p6.get('reasons_citing_time')}/{len(p6.get('reasons', []))}",
            "p5_score": p5 and p5.get("score"),
            "p6_score": p6 and p6.get("score"),
            "features": checks,
        })

    def acc(col, exp):
        scored = [r for r in rows if r[col] is not None and r[exp] is not None]
        ok = sum(r[col] == r[exp] for r in scored)
        return {"correct": ok, "n": len(scored), "accuracy": round(ok / len(scored), 3) if scored else None}

    return {
        "videos": rows,
        "verdict_accuracy": {
            "P5 text-only, model verdict": acc("p5_model", "expected"),
            "P5 text-only, after rule guard": acc("p5_final", "expected"),
            "P6 multimodal, model verdict": acc("p6_model", "expected_multimodal"),
            "P6 multimodal, after rule guard": acc("p6_final", "expected_multimodal"),
        },
        "feature_accuracy": {k: dict(v, accuracy=round(v["correct"] / v["n"], 3))
                             for k, v in feat_totals.items()},
        "reviewer_timing": reviewer_timing(),
    }
