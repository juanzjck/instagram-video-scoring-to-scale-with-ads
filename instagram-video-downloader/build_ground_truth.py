"""Generate ground_truth.csv (the answer key) for the dataset built by build_dataset.py.

The real answer key is what happened after paying to scale each video: ad spend, conversions,
CAC. Nobody scaled these videos, so this script SIMULATES that outcome. Every note starts with
"SIMULATED" so the file is never mistaken for real results.

1. guard rules, same thresholds as full-workflow-api (no outcome is simulated for these):
     missing_metrics    reach or views blank                         -> DEFER_TO_HUMAN
     too_new            under 48 h since posting                     -> DEFER_TO_HUMAN
     tiny_reach         reach under 1,000                            -> DEFER_TO_HUMAN
     inorganic          more likes than reach, or many likes and
                        almost no saves                              -> FLAG_FRAUD
     audience_mismatch  under 10 % of followers in the target
                        countries (only with --target-countries)     -> DO_NOT_SCALE
2. outcome for the rest: CAC is drawn around the target CAC and pulled down by buying intent
   in the comments, save rate and share rate, and up by skip rate. Raw engagement counts only
   a little, so a viral video can still convert badly (that is how the traps appear).
   Seeded by video_id, so every run gives the same key.
3. labels: expected_verdict SCALE when CAC <= target CAC, otherwise DO_NOT_SCALE; scenario from
   the CAC vs target and the engagement rank (clear_winner, clear_loser, borderline,
   trap_high_engagement, trap_quiet_seller).

The answer key must be fixed BEFORE the pipeline runs, so an existing ground_truth.csv is never
overwritten unless you pass --force.

Usage:
    python build_ground_truth.py
    python build_ground_truth.py --target-cac 40 --value-per-conversion 60 --target-countries CO,EC,PE
"""

import argparse
import csv
import json
import math
import random
import statistics
from pathlib import Path

MIN_HOURS = 48     # full-workflow-api config.MIN_HOURS
MIN_REACH = 1000   # full-workflow-api config.MIN_REACH
MIN_AUDIENCE_SHARE = 0.10

# comment phrases that show someone wants to sign up / buy (es + en)
INTENT_WORDS = ["cómo", "como abro", "abrir cuenta", "link", "info", "información", "quiero",
                "descarg", "me registr", "ya invertí", "ya abrí", "dónde", "donde", "precio",
                "cuánto", "cuanto", "asesor", "how do i", "where", "sign up", "price", "bought"]


def read_rows(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_header(path):
    with path.open(newline="", encoding="utf-8") as f:
        return next(csv.reader(f))


def num(v):
    return None if v in ("", None) else float(v)


def zscores(values):
    """Standard scores; 0 for everyone when there is no spread."""
    mean = statistics.mean(values)
    sd = statistics.pstdev(values)
    return [(v - mean) / sd if sd else 0.0 for v in values]


def fmt(x, digits=2):
    return f"{x:.{digits}f}".rstrip("0").rstrip(".")


def guard(v, creator, target_countries):
    """(scenario, verdict, note) when a guard rule applies, else None."""
    reach, views, likes, saved = num(v["reach"]), num(v["views"]), num(v["likes"]), num(v["saved"])
    hours = num(v["hours_since_post"])
    if reach is None or views is None:
        return "missing_metrics", "DEFER_TO_HUMAN", "Reach or views blank; rates cannot be computed."
    if hours is not None and hours < MIN_HOURS:
        return "too_new", "DEFER_TO_HUMAN", f"Only {fmt(hours, 1)}h since posting; insights lag up to 48h."
    if reach < MIN_REACH:
        return "tiny_reach", "DEFER_TO_HUMAN", f"{reach:,.0f} reach is below the {MIN_REACH:,} threshold."
    if likes and (likes > reach or (likes / reach > 0.25 and (saved or 0) / likes < 0.01)):
        return "inorganic", "FLAG_FRAUD", f"{likes:,.0f} likes on {reach:,.0f} reach with {saved or 0:,.0f} saves."
    if target_countries:
        try:
            countries = json.loads(creator.get("follower_demographics_country") or "{}")
        except json.JSONDecodeError:
            countries = {}
        followers = num(creator.get("followers"))
        if countries and followers:
            share = sum(countries.get(c, 0) for c in target_countries) / followers
            if share < MIN_AUDIENCE_SHARE:
                return ("audience_mismatch", "DO_NOT_SCALE",
                        f"Only {share:.1%} of followers are in {'|'.join(target_countries)}.")
    return None


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=str(here / "dataset"),
                    help="folder with videos.csv, creators.csv, comments.csv (default: dataset)")
    ap.add_argument("--template", default=str(here.parent / "data-template"),
                    help="folder whose ground_truth.csv header is copied (read only)")
    ap.add_argument("--out", default="", help="output file (default: <dataset>/ground_truth.csv)")
    ap.add_argument("--target-cac", type=float, default=None,
                    help="max USD per conversion (default: campaigns.csv in the dataset, else 40)")
    ap.add_argument("--target-countries", default="",
                    help="e.g. CO,EC,PE (default: campaigns.csv in the dataset; blank skips the check)")
    ap.add_argument("--value-per-conversion", type=float, default=60.0,
                    help="USD earned per conversion, for ROAS (default 60)")
    ap.add_argument("--spend-per-1k-reach", type=float, default=100.0,
                    help="USD spent scaling, per 1,000 organic reach (default 100, kept within 300–3000)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing ground_truth.csv")
    args = ap.parse_args()

    ds = Path(args.dataset)
    out = Path(args.out) if args.out else ds / "ground_truth.csv"
    if out.exists() and not args.force:
        ap.error(f"{out} already exists. The answer key must not change after the pipeline ran; "
                 "pass --force to replace it anyway.")

    videos = read_rows(ds / "videos.csv")
    if not videos:
        ap.error(f"no videos in {ds / 'videos.csv'}; run build_dataset.py first")
    creators = {c["creator_id"]: c for c in read_rows(ds / "creators.csv")}
    comments = read_rows(ds / "comments.csv")
    campaign = (read_rows(ds / "campaigns.csv") or [{}])[0]

    target_cac = args.target_cac or num(campaign.get("target_cac")) or 40.0
    countries = args.target_countries or campaign.get("target_countries", "")
    target_countries = [c.strip().upper() for c in countries.replace("|", ",").split(",") if c.strip()]

    rows, scored = [], []
    for v in videos:
        g = guard(v, creators.get(v["creator_id"], {}), target_countries)
        row = {"video_id": v["video_id"], "scenario": "", "expected_verdict": "",
               "performed_well_after_scaling": "", "ad_spend": "", "conversions": "", "cac": "",
               "roas": "", "notes": ""}
        if g:
            row["scenario"], row["expected_verdict"], note = g
            row["notes"] = f"SIMULATED key, not scaled: {note}"
        else:
            scored.append((v, row))
        rows.append(row)

    if scored:
        feats = []
        for v, _ in scored:
            reach = num(v["reach"])
            texts = [c["text"].lower() for c in comments if c["video_id"] == v["video_id"]]
            intent = sum(any(w in t for w in INTENT_WORDS) for t in texts) / len(texts) if texts else 0.0
            feats.append({
                "eng": (num(v["total_interactions"]) or 0) / reach,
                "save": (num(v["saved"]) or 0) / reach,
                "share": (num(v["shares"]) or 0) / reach,
                "skip": num(v["skip_rate_pct"]) or 0.0,
                "intent": intent, "n_comments": len(texts),
            })
        z = {k: zscores([f[k] for f in feats]) for k in ("eng", "save", "share", "skip", "intent")}
        eng_median = statistics.median(f["eng"] for f in feats)

        for i, (v, row) in enumerate(scored):
            rng = random.Random(f"gt:{v['video_id']}")
            quality = (0.35 * z["intent"][i] + 0.25 * z["save"][i] + 0.15 * z["share"][i]
                       - 0.20 * z["skip"][i] + 0.05 * z["eng"][i])
            cac_ratio = math.exp(0.10 - 0.45 * quality + rng.gauss(0, 0.30))
            spend = min(max(round(num(v["reach"]) / 1000 * args.spend_per_1k_reach, -1), 300), 3000)
            conversions = max(round(spend / (target_cac * cac_ratio)), 0)
            cac = spend / conversions if conversions else None
            roas = conversions * args.value_per_conversion / spend

            f = feats[i]
            high_eng = f["eng"] >= eng_median
            good = cac is not None and cac <= target_cac
            r = cac / target_cac if cac else float("inf")
            if good:
                scenario = ("clear_winner" if r <= 0.8 else "borderline") if high_eng else "trap_quiet_seller"
            else:
                scenario = "trap_high_engagement" if high_eng else ("clear_loser" if r >= 1.25 else "borderline")

            row.update({
                "scenario": scenario,
                "expected_verdict": "SCALE" if good else "DO_NOT_SCALE",
                "performed_well_after_scaling": "yes" if good else "no",
                "ad_spend": fmt(spend), "conversions": conversions,
                "cac": fmt(cac) if cac else "", "roas": fmt(roas),
                "notes": (f"SIMULATED outcome. {f['eng']:.1%} engagement, {f['save']:.2%} save rate, "
                          f"{f['share']:.2%} share rate, {f['skip']:.0f}% skip; "
                          f"{f['intent']:.0%} of {f['n_comments']} comments show buying intent. "
                          f"CAC {fmt(cac) if cac else 'n/a'} vs target {fmt(target_cac)}."),
            })

    out.parent.mkdir(parents=True, exist_ok=True)
    cols = read_header(Path(args.template) / "ground_truth.csv")
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    counts = {}
    for r in rows:
        counts[r["scenario"]] = counts.get(r["scenario"], 0) + 1
    print(f"Done: {len(rows)} videos -> {out} (target CAC {fmt(target_cac)}"
          f"{', countries ' + '|'.join(target_countries) if target_countries else ''})")
    print("Scenarios: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))


if __name__ == "__main__":
    main()
