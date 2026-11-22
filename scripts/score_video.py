"""Score creator videos from the command line - the same pipeline the web app runs.

    input  -> a saved session id and one or more video ids
    API    -> Claude vision (keyframes) + Claude text scoring (prompt P6)
    output -> verdict, score, range and reasons, printed and saved as JSON

Usage (from full-workflow-api/):
    python scripts/score_video.py --list                      # sessions and their videos
    python scripts/score_video.py SESSION_ID VIDEO_ID [...]   # score (uses cache)
    python scripts/score_video.py SESSION_ID --all --force    # re-run every video
    python scripts/score_video.py SESSION_ID VIDEO_ID --summary   # show the prompt input, no API call
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, ingest, pipeline  # noqa: E402


def list_sessions():
    for d in sorted(p for p in config.UPLOADS.iterdir() if p.is_dir() and config.DATASET_ID_RE.match(p.name)):
        try:
            config.activate(d.name)
            ingest.load_inputs.cache_clear()
            vids = list(ingest.load_inputs()[0])
        except Exception as e:                       # an empty or half-built session
            vids = [f"(unreadable: {e})"]
        print(f"{d.name}: {', '.join(vids) or '(no videos)'}")


def score(video_id, force, prompt_id=None):
    """Run the pipeline for one video and return a compact result."""
    card = pipeline.process(video_id, force=force, prompt_id=prompt_id,
                            progress=lambda step: print(f"  {video_id}: {step}", file=sys.stderr))
    s = card.get("score") or {}
    return {
        "video_id": video_id,
        "prompt_id": s.get("prompt_id") or prompt_id,
        "verdict": s.get("verdict"),
        "model_verdict": s.get("model_verdict"),
        "score": s.get("score"),
        "range": [s.get("score_low"), s.get("score_high")],
        "confidence": s.get("confidence"),
        "rule_hits": s.get("rule_hits"),
        "reasons": s.get("reasons"),
        "injection_flags": len(card.get("injection_flags") or []),
        "tokens": (s.get("usage") or {}),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", nargs="?")
    ap.add_argument("videos", nargs="*")
    ap.add_argument("--all", action="store_true", help="every video in the session")
    ap.add_argument("--force", action="store_true", help="ignore cached results and call the API again")
    ap.add_argument("--prompt", default=None, help="P6-family prompt id (P6, P6.2, ...); default: the session's active one. Not adopted.")
    ap.add_argument("--summary", action="store_true", help="print the text the model receives; no API call")
    ap.add_argument("--list", action="store_true", help="list saved sessions")
    a = ap.parse_args()

    if a.list or not a.session:
        return list_sessions()

    try:
        config.activate(a.session)
    except ValueError as e:
        sys.exit(f"error: {e} (run with --list)")
    known = list(ingest.load_inputs()[0])
    ids = known if a.all else a.videos
    unknown = [v for v in ids if v not in known]
    if not ids or unknown:
        sys.exit(f"error: unknown or missing video ids {unknown or ''}. Known: {', '.join(known)}")

    print("note: this scores in place and records no run; use the app's Start pipeline (or scripts/run_collection_test.py) "
          "for runs that are kept apart per prompt and model.", file=sys.stderr)
    if a.prompt:
        from app import prompt_versions as pv
        if a.prompt == pv.BASELINE or not any(x["id"] == a.prompt for x in pv.listing()["versions"]):
            sys.exit(f"error: unknown or unusable prompt {a.prompt!r} (a P6-family id of this session, not P5)")

    if a.summary:
        for v in ids:
            print(pipeline.summary(v, multimodal=True), "\n")
        return

    results, failed = [], 0
    for v in ids:
        try:
            results.append(score(v, a.force, a.prompt))
        except Exception as e:                       # one bad video must not stop the batch
            failed += 1
            results.append({"video_id": v, "error": f"{type(e).__name__}: {e}"})

    out = config.RUNS / "cli_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        if "error" in r:
            print(f"{r['video_id']}: ERROR {r['error']}")
        else:
            print(f"{r['video_id']}: {r['verdict']} (score {r['score']}, range {r['range'][0]}-{r['range'][1]}, "
                  f"{r['confidence']})" + (f"  rule guard: {'; '.join(r['rule_hits'])}" if r["rule_hits"] else ""))
    print(f"\nsaved {out}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
