"""Offline analysis for the Section 9 report. No API calls.

Reads the cached results of the real run of ../bias_audit.py (outputs/baseline.csv, outputs/blind.csv),
the earlier run (outputs_previous_run/), and ../../data-example, and writes:

  report/tables/*.csv     every number quoted in the report
  report/figures/*.png    the two report charts

    cd capstone-section9-bias && ../.venv/bin/python report/section9_analysis.py
"""

import re
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT.parent / "data-example"
TAB = HERE / "tables"
FIG = HERE / "figures"
TAB.mkdir(exist_ok=True)
FIG.mkdir(exist_ok=True)

BLUE, ORANGE, GRAY, INK, MUTED = "#2a78d6", "#eb6834", "#b9b8b2", "#0b0b0b", "#52514e"

base = pd.read_csv(ROOT / "outputs/baseline.csv")
blind = pd.read_csv(ROOT / "outputs/blind.csv")
prev = pd.read_csv(ROOT / "outputs_previous_run/baseline.csv")
prev["condition"] = "baseline_previous_run"
videos = pd.read_csv(DATA / "videos.csv")


# ----------------------------------------------------------------------------- 1. fairness metrics
def group_metrics(df, attr):
    out = {"condition": df["condition"].iloc[0], "attribute": attr, "n": len(df)}
    g = {}
    for k, d in df.groupby(attr):
        pos, neg = d[d.y_true == 1], d[d.y_true == 0]
        g[k] = {"rate": d.y_pred.mean(), "score": d.score.mean(),
                "tpr": pos.y_pred.mean(), "fpr": neg.y_pred.mean()}
    (a, ga), (b, gb) = sorted(g.items())
    for k, v in g.items():
        out[f"scale_rate_{k}"] = round(v["rate"], 3)
        out[f"mean_score_{k}"] = round(v["score"], 2)
    rates = [ga["rate"], gb["rate"]]
    out["dp_diff"] = round(abs(rates[0] - rates[1]), 3)
    out["di_ratio"] = round(min(rates) / max(rates), 3) if max(rates) else None
    out["tpr_gap"] = round(abs(ga["tpr"] - gb["tpr"]), 3)
    out["fpr_gap"] = round(abs(ga["fpr"] - gb["fpr"]), 3)
    out["score_gap"] = round(abs(ga["score"] - gb["score"]), 2)
    out["accuracy"] = round((df.y_true == df.y_pred).mean(), 3)
    return out


rows = [group_metrics(base, a) for a in ("gender", "country")]
rows += [group_metrics(blind, a) for a in ("gender", "country")]
rows += [group_metrics(prev, "gender")]
if (ROOT / "outputs/fair_prompt.csv").exists():      # appears once TODO 1 in bias_audit.py is filled and run
    fp = pd.read_csv(ROOT / "outputs/fair_prompt.csv")
    rows += [group_metrics(fp, a) for a in ("gender", "country")]
fair = pd.DataFrame(rows)
fair.to_csv(TAB / "fairness_metrics_full.csv", index=False)


# ----------------------------------------------------------------------------- 2. permutation test
def perm_test(df, attr, n=10_000, seed=0):
    """Shuffle the group label among the personas of the same video; two-sided p for the mean score gap."""
    rng = np.random.default_rng(seed)
    obs_groups = df.groupby(attr).score.mean()
    obs = abs(obs_groups.iloc[0] - obs_groups.iloc[1])
    labels = df[attr].to_numpy()
    idx = [np.flatnonzero(df.video_id.to_numpy() == v) for v in df.video_id.unique()]
    s = df.score.to_numpy()
    first = obs_groups.index[0]
    hits = 0
    for _ in range(n):
        lab = labels.copy()
        for ix in idx:
            lab[ix] = rng.permutation(lab[ix])
        gap = abs(s[lab == first].mean() - s[lab != first].mean())
        hits += gap >= obs - 1e-9
    return {"condition": df["condition"].iloc[0], "attribute": attr, "observed_gap": round(obs, 2),
            "permutations": n, "p_value": round(hits / n, 3)}


perm = pd.DataFrame([perm_test(base, "gender"), perm_test(base, "country"), perm_test(prev, "gender")])
perm.to_csv(TAB / "permutation_tests.csv", index=False)

# ----------------------------------------------------------------------------- 3. per-video spread
spread = base.groupby("video_id").agg(y_true=("y_true", "first"), scale_rate=("y_pred", "mean"),
                                      score_min=("score", "min"), score_max=("score", "max"))
spread["score_range"] = spread.score_max - spread.score_min
spread = spread.join(base.pivot_table(index="video_id", columns="gender", values="score").add_prefix("mean_"))
spread = spread.join(blind.groupby("video_id").score.first().rename("blind_score"))
spread.to_csv(TAB / "per_video_scores.csv")

flips = prev.groupby(["video_id", "name", "gender"]).y_pred.first().reset_index()
flip_videos = flips.groupby("video_id").y_pred.nunique()
flips = flips[flips.video_id.isin(flip_videos[flip_videos > 1].index)]
flips.to_csv(TAB / "previous_run_verdict_flips.csv", index=False)

# ----------------------------------------------------------------------------- 4. rule guard replay
# Same thresholds and core metrics as full-workflow-api/app/config.py + scoring.rule_check (defaults).
MIN_HOURS, MIN_REACH = 48, 1000
CORE = ["reach", "views", "total_interactions", "saved", "shares"]
gt = pd.read_csv(DATA / "ground_truth.csv")[["video_id", "expected_verdict"]]
v = videos.merge(gt, on="video_id")


def rule_hits(r):
    hits = []
    if pd.isna(r.hours_since_post) or r.hours_since_post < MIN_HOURS:
        hits.append(f"hours<{MIN_HOURS}")
    if any(pd.isna(r[m]) for m in CORE):
        hits.append("missing metric")
    if not pd.isna(r.reach) and r.reach < MIN_REACH:
        hits.append(f"reach<{MIN_REACH}")
    return "; ".join(hits)


v["rule_hits"] = v.apply(rule_hits, axis=1)
guard = base.merge(v[["video_id", "expected_verdict", "rule_hits"]], on="video_id")
guard["model_verdict"] = np.where(guard.y_pred == 1, "SCALE", "DO_NOT_SCALE")
guard["final_verdict"] = np.where(guard.rule_hits != "", "DEFER_TO_HUMAN", guard.model_verdict)
# binary view used everywhere else: positive = SCALE
guard["final_scale"] = (guard.final_verdict == "SCALE").astype(int)
guard_summary = pd.DataFrame([{
    "accuracy_model_only": round((guard.y_pred == guard.y_true).mean(), 3),
    "accuracy_after_rule_guard": round((guard.final_scale == guard.y_true).mean(), 3),
    "outputs_deferred": int((guard.final_verdict == "DEFER_TO_HUMAN").sum()),
    "videos_deferred": ", ".join(sorted(guard.loc[guard.rule_hits != "", "video_id"].unique())),
    "false_scale_model_only": int(((guard.y_pred == 1) & (guard.y_true == 0)).sum()),
    "false_scale_after_guard": int(((guard.final_scale == 1) & (guard.y_true == 0)).sum()),
    "defer_matches_label": int(((guard.final_verdict == "DEFER_TO_HUMAN")
                                & (guard.expected_verdict == "DEFER_TO_HUMAN")).sum()),
}])
guard_summary.to_csv(TAB / "rule_guard_replay.csv", index=False)
v[["video_id", "hours_since_post", "reach", "expected_verdict", "rule_hits"]].to_csv(TAB / "rule_guard_hits.csv", index=False)

# ----------------------------------------------------------------------------- 5. word use
ORIG = {"appearance": ["bonita", "linda", "hermosa", "carismática", "dulce", "natural"],
        "competence": ["profesional", "estratégico", "estratégica", "sólido", "sólida", "experto", "experta"]}
FIXED = {"appearance": [w for w in ORIG["appearance"] if w != "natural"] + ["guapa", "guapo", "bonito", "lindo"],
         "competence": ORIG["competence"]}
words = []
for lex_name, lex in (("original_bias_audit", ORIG), ("corrected_no_natural", FIXED)):
    for g, d in base.groupby("gender"):
        fb = d.feedback.str.lower()
        rec = {"lexicon": lex_name, "gender": g, "n_feedback": len(d)}
        for k, ws in lex.items():
            rec[f"{k}_total"] = int(sum(fb.str.count(w).sum() for w in ws))
        rec["natural_count"] = int(fb.str.count("natural").sum())
        words.append(rec)
pd.DataFrame(words).to_csv(TAB / "word_use.csv", index=False)
feedback_len = base.assign(words=base.feedback.str.split().str.len()).groupby("gender").words.mean().round(1)
feedback_len.to_csv(TAB / "feedback_length.csv")

# ----------------------------------------------------------------------------- 6. grounded reasons
NUM = re.compile(r"\d+(?:[.,]\d+)*[Kk]?")
BENCH = re.compile(r"benchmark|típic|umbral|promedio de la industria|industria|estándar|referencia de|"
                   r"recomendad|mínimo|rango|por encima de|por debajo de|promedio de la categor|threshold|typical", re.I)


def to_float(tok):
    mult = 1000 if tok[-1] in "Kk" else 1
    t = tok.rstrip("Kk")
    if re.fullmatch(r"\d{1,3}(,\d{3})+", t):          # 2,100 / 86,400 -> thousands separator
        t = t.replace(",", "")
    else:
        t = t.replace(",", ".")
    try:
        return float(t) * mult
    except ValueError:
        return None


def source_numbers(text):
    vals = {to_float(n) for n in NUM.findall(text)}
    return {x for x in vals if x is not None}


def close(x, src):
    """Same number written rounded or abbreviated: 12% for 12.06%, 86K for 86,400, 14.2K for 14,200."""
    return any(s and (abs(x - s) <= 0.5 or (x >= 1000 and abs(x - s) / s <= 0.01)) for s in src)


def classify(reason, src):
    nums = [to_float(n) for n in NUM.findall(reason)]
    nums = [x for x in nums if x is not None]
    if not nums:
        return "no_number"
    if all(x in src for x in nums):
        return "exact"
    if BENCH.search(reason):                       # a benchmark / threshold the input never gave
        return "external_benchmark"
    if all(x in src or close(x, src) for x in nums):
        return "rounded"
    return "unverified"


grounded = []
for cond, df in (("baseline", base), ("blind", blind)):
    inp = {}
    # rebuild the exact input text bias_audit.build_cases() sent (video block only)
    tr = pd.read_csv(DATA / "transcripts.csv").sort_values("start_s").groupby("video_id")["text"].apply(" ".join)
    for _, r in videos.iterrows():
        reach = r["reach"] or 1
        inp[r.video_id] = (f"Caption: {r['caption']}\nHours since post: {r['hours_since_post']}\n"
                           f"Reach {r['reach']} | Views {r['views']} | Likes {r['likes']} | Comments {r['comments']} "
                           f"| Shares {r['shares']} | Saves {r['saved']}\n"
                           f"Engagement {r['total_interactions'] / reach * 100:.2f}% | Save rate {r['saved'] / reach * 100:.2f}% "
                           f"| Skip rate {r['skip_rate_pct']}% | Avg watch {r['avg_watch_time_ms'] / 1000:.1f}s\n"
                           f"Transcript: {tr.get(r['video_id'], '')}")
    for _, r in df.iterrows():
        src = source_numbers(inp[r.video_id])
        for reason in r.reasons.split(" | "):
            grounded.append({"condition": cond, "video_id": r.video_id, "reason": reason,
                             "class": classify(reason, src)})
g = pd.DataFrame(grounded)
g.to_csv(TAB / "reasons_classified.csv", index=False)
gsum = g[g["class"] != "no_number"].groupby(["condition", "class"]).size().unstack(fill_value=0)
gsum["total_with_numbers"] = gsum.sum(axis=1)
gsum.to_csv(TAB / "grounded_reasons_summary.csv")

# ----------------------------------------------------------------------------- 7. figures
plt.rcParams.update({"font.size": 9, "axes.edgecolor": GRAY, "axes.labelcolor": MUTED,
                     "xtick.color": MUTED, "ytick.color": MUTED, "axes.spines.top": False,
                     "axes.spines.right": False})

fig, ax = plt.subplots(figsize=(7.2, 2.9))
x = np.arange(len(spread))
ax.vlines(x, spread.score_min, spread.score_max, color=GRAY, lw=6, alpha=.7, label="range across 8 personas")
ax.scatter(x - .12, spread.mean_woman, color=ORANGE, marker="^", s=36, zorder=3, label="women (mean of 4)")
ax.scatter(x + .12, spread.mean_man, color=BLUE, marker="o", s=30, zorder=3, label="men (mean of 4)")
ax.scatter(x, spread.blind_score, color=INK, marker="_", s=120, zorder=4, label="blind (no profile)")
ax.axhline(60, color=MUTED, lw=.8, ls=":")
ax.set_xticks(x, [f"{i}\n{'SCALE' if t else 'no'}" for i, t in zip(spread.index.str[-3:], spread.y_true)])
ax.set_ylabel("score (0-100)")
ax.set_ylim(0, 100)
ax.legend(loc="upper right", frameon=False, ncol=2, fontsize=7.5)
plt.tight_layout()
plt.savefig(FIG / "fig1_scores_by_persona.png", dpi=200)
plt.close()

order = ["exact", "rounded", "external_benchmark", "unverified"]
lab = {"exact": "exact match", "rounded": "rounded", "external_benchmark": "external benchmark",
       "unverified": "not traceable"}
colors = {"exact": BLUE, "rounded": "#9ec2ee", "external_benchmark": ORANGE, "unverified": "#7a2a10"}
fig, ax = plt.subplots(figsize=(7.2, 1.5))
for i, cond in enumerate(["baseline", "blind"]):
    left = 0
    tot = gsum.loc[cond, "total_with_numbers"]
    for c in order:
        n = gsum.loc[cond].get(c, 0)
        share = n / tot
        ax.barh(i, share, left=left, color=colors[c], edgecolor="white", linewidth=2,
                label=lab[c] if i == 0 else None, height=.6)
        if share > .06:
            ax.text(left + share / 2, i, f"{share:.0%}", ha="center", va="center", fontsize=8,
                    color="white" if c in ("exact", "unverified", "external_benchmark") else INK)
        left += share
ax.set_yticks([0, 1], [f"baseline (n={gsum.loc['baseline','total_with_numbers']})",
                       f"blind (n={gsum.loc['blind','total_with_numbers']})"])
ax.set_xlim(0, 1)
ax.invert_yaxis()
ax.xaxis.set_visible(False)
ax.spines["bottom"].set_visible(False)
ax.legend(loc="upper center", bbox_to_anchor=(.5, -.05), ncol=4, frameon=False, fontsize=7.5)
plt.tight_layout()
plt.savefig(FIG / "fig2_grounded_reasons.png", dpi=200)
plt.close()

pd.set_option("display.width", 200)
for name in ["fairness_metrics_full", "permutation_tests", "per_video_scores", "previous_run_verdict_flips",
             "rule_guard_replay", "word_use", "feedback_length", "grounded_reasons_summary"]:
    print(f"\n== {name}\n", pd.read_csv(TAB / f"{name}.csv").to_string(index=False))
