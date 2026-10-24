"""Evaluate. The only module that opens the answer keys.

1. Verdicts: text-only P5 vs multimodal P6 against ground truth.
2. Content features: the vision output against ground_truth_media.csv.
3. Reviewer time: how long a human took to decide with the card vs with text only,
   read from the decision log.
4. Manual review: the blind human decisions (manual_review.csv) vs P5 / P6 on the same videos,
   both against the ground truth's verdict and against its CAC outcome.
"""

import json
from statistics import mean, median

import pandas as pd

from . import config, ingest, pipeline, scoring
from .ingest import num, val


def _key(name):
    if config.DATA is None:
        return {}
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


def _manual():
    if config.DATA is None or not (config.DATA / "manual_review.csv").exists():
        return []
    df = pd.read_csv(config.DATA / "manual_review.csv", dtype={"video_id": str, "reviewer": str})
    return [{k: val(v) for k, v in r.items()} for _, r in df.iterrows()]


def _scale(verdict):
    """A verdict as the reviewer's two choices: SCALE, or anything else (defer, flag) = DO_NOT_SCALE."""
    return None if verdict is None else ("SCALE" if verdict == "SCALE" else "DO_NOT_SCALE")


def _cac_call(gt):
    """The call the real CAC supports: scale when it performed well (CAC ≤ target), blank if never scaled."""
    return {"yes": "SCALE", "no": "DO_NOT_SCALE"}.get(str((gt or {}).get("performed_well_after_scaling") or "").lower())


def manual_vs_ai(rows, manual):
    """Per reviewer: their accuracy and the AI's on exactly the videos they reviewed, plus minutes spent."""
    by_vid = {r["video_id"]: r for r in rows}
    out = []
    for name in dict.fromkeys(m["reviewer"] for m in manual):
        mine = [m for m in manual if m["reviewer"] == name and m["video_id"] in by_vid]
        mins = [x for x in (num(m.get("minutes_spent")) for m in mine) if x is not None]
        entry = {"reviewer": name, "videos": len(mine),
                 "mean_minutes": round(mean(mins), 1) if mins else None,
                 "total_minutes": round(sum(mins), 1) if mins else None}
        for measure, key in (("expected_verdict", "expected_scale"), ("cac_outcome", "cac_call")):
            judged = [(m, by_vid[m["video_id"]]) for m in mine if by_vid[m["video_id"]][key] is not None]

            def acc(pick):
                got = [(pick(m, v), v[key]) for m, v in judged]
                got = [(g, e) for g, e in got if g is not None]
                ok = sum(g == e for g, e in got)
                return {"correct": ok, "n": len(got), "accuracy": round(ok / len(got), 3) if got else None}

            entry[measure] = {"human": acc(lambda m, v: m.get("decision")),
                              "p5_final": acc(lambda m, v: _scale(v["p5_final"])),
                              "p6_final": acc(lambda m, v: _scale(v["p6_final"]))}
        out.append(entry)
    return out


def confusion(pairs):
    """Precision, recall, F1 and accuracy for (decision, truth) pairs, SCALE being the positive class:
    a winner is a video the truth says to scale. Pairs where either side is missing are skipped."""
    pairs = [(p, t) for p, t in pairs if p is not None and t is not None]
    tp = sum(p == "SCALE" and t == "SCALE" for p, t in pairs)
    fp = sum(p == "SCALE" and t != "SCALE" for p, t in pairs)
    fn = sum(p != "SCALE" and t == "SCALE" for p, t in pairs)
    tn = len(pairs) - tp - fp - fn
    ratio = lambda a, b: round(a / b, 3) if b else None
    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    f1 = ratio(2 * tp, 2 * tp + fp + fn)      # = 2PR/(P+R), and 0 when no winner is caught
    return {"n": len(pairs), "tp": tp, "fp": fp, "fn": fn, "tn": tn, "recall": recall, "precision": precision,
            "f1": f1, "accuracy": ratio(tp + tn, len(pairs)), "winners": tp + fn}


def classification(rows, manual):
    """The three methods (blind human, P5, P6 after the rule guard) as scale / don't-scale classifiers.

    Measured against the same truth for all three: the ground truth's expected verdict, and the CAC
    outcome. Two scopes: every video each method decided, and only the videos the reviewers decided,
    where each human decision is paired with the AI's call on that same video (n is equal once both
    AI prompts have scored every reviewed video).
    """
    by_vid = {r["video_id"]: r for r in rows}
    human = [(m, by_vid[m["video_id"]]) for m in manual
             if m["video_id"] in by_vid and m.get("decision")]
    out = {}
    for measure, key in (("expected_verdict", "expected_scale"), ("cac_outcome", "cac_call")):
        truths = [r[key] for r in rows if r[key] is not None]
        winners = sum(t == "SCALE" for t in truths)
        out[measure] = {
            "all": {"Human (blind)": confusion((m["decision"], v[key]) for m, v in human),
                    "P5 text-only": confusion((_scale(r["p5_final"]), r[key]) for r in rows),
                    "P6 multimodal": confusion((_scale(r["p6_final"]), r[key]) for r in rows)},
            "same": {"Human (blind)": confusion((m["decision"], v[key]) for m, v in human),
                     "P5 text-only": confusion((_scale(v["p5_final"]), v[key]) for m, v in human),
                     "P6 multimodal": confusion((_scale(v["p6_final"]), v[key]) for m, v in human)},
            "videos": len(truths), "winners": winners,
            # what accuracy a method gets by never scaling anything: the reason accuracy carries a warning
            "never_scale_accuracy": round(1 - winners / len(truths), 3) if truths else None}
    return out


def read_decisions():
    if config.DECISIONS_LOG is None or not config.DECISIONS_LOG.exists():
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


def _stats(xs):
    return {"n": len(xs), "mean_s": round(mean(xs), 1) if xs else None,
            "median_s": round(median(xs), 1) if xs else None, "total_s": round(sum(xs), 1) if xs else None}


def pipeline_timing(video_ids, manual):
    """Seconds per pipeline stage across the campaign's videos, and the end-to-end time per video
    (download when there was one + preprocess + analyse + P6), next to the blind reviewers' minutes."""
    timings = {vid: pipeline.load(vid, "timing") or {} for vid in video_ids}
    stages = {s: _stats([t[s] for t in timings.values() if t.get(s) is not None]) for s in pipeline.TIMED_STAGES}
    full = {vid: round(sum(t.get(s) or 0 for s in ("download", "preprocess", "analyse", "score_p6")), 1)
            for vid, t in timings.items() if all(t.get(s) is not None for s in ("preprocess", "analyse", "score_p6"))}
    human = [x for x in (num(m.get("minutes_spent")) for m in manual) if x is not None]
    return {"stages": stages, "per_video": full, "end_to_end": _stats(list(full.values())),
            "human": {"n": len(human), "mean_s": round(mean(human) * 60, 1) if human else None,
                      "median_s": round(median(human) * 60, 1) if human else None}}


def run(run_missing=True, reviewers=None):
    """Build the evaluation report. With run_missing, videos not yet scored are processed.
    reviewers: names whose blind decisions count as "Human"; None means every submitted reviewer."""
    videos, _ = ingest.load_inputs()
    gt = _key("ground_truth.csv")
    gt_media = _key("ground_truth_media.csv")
    every = _manual()
    names = list(dict.fromkeys(m["reviewer"] for m in every))
    picked = names if reviewers is None else [n for n in names if n in set(reviewers)]
    manual = [m for m in every if m["reviewer"] in picked]

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
            "expected_scale": _scale(expected),
            "cac_call": _cac_call(gt.get(vid)),
            "cac": num((gt.get(vid) or {}).get("cac")),
            "manual": {m["reviewer"]: m.get("decision") for m in manual if m["video_id"] == vid},
        })

    def _recall(scored, col, truth):
        """Of the winners (truth says SCALE), how many the verdict scaled."""
        winners = [r for r in scored if truth(r) == "SCALE"]
        tp = sum(r[col] == "SCALE" for r in winners)
        return {"tp": tp, "winners": len(winners), "recall": round(tp / len(winners), 3) if winners else None}

    def acc(col, exp):
        scored = [r for r in rows if r[col] is not None and r[exp] is not None]
        ok = sum(r[col] == r[exp] for r in scored)
        return {"correct": ok, "n": len(scored), "accuracy": round(ok / len(scored), 3) if scored else None,
                **_recall(scored, col, lambda r: r[exp])}

    def vs_cac(col, exp):
        """acc() against the expected verdict, plus the same verdicts against the real CAC outcome
        (scale / don't scale; videos never scaled are left out)."""
        scored = [r for r in rows if r[col] is not None and r["cac_call"] is not None]
        ok = sum(_scale(r[col]) == r["cac_call"] for r in scored)
        return dict(acc(col, exp), cac={"correct": ok, "n": len(scored),
                                        "accuracy": round(ok / len(scored), 3) if scored else None,
                                        **_recall(scored, col, lambda r: r["cac_call"])})

    return {
        "videos": rows,
        "verdict_accuracy": {
            "P5 text-only, model verdict": vs_cac("p5_model", "expected"),
            "P5 text-only, after rule guard": vs_cac("p5_final", "expected"),
            "P6 multimodal, model verdict": vs_cac("p6_model", "expected_multimodal"),
            "P6 multimodal, after rule guard": vs_cac("p6_final", "expected_multimodal"),
        },
        "feature_accuracy": {k: dict(v, accuracy=round(v["correct"] / v["n"], 3))
                             for k, v in feat_totals.items()},
        "manual_review": manual_vs_ai(rows, manual),
        "reviewer_timing": reviewer_timing(),
        "pipeline_timing": pipeline_timing(list(videos), manual),
        "classification": classification(rows, manual),
        "reviewers": {"all": names, "selected": picked},
    }
