"""Repeat the whole workflow on one campaign many times and measure how stable the verdicts are.

Creates (or resumes) a collection of N copies ("runs") of a source session, sets each run's LLM, and
per run: checks the copied preprocessing (keyframes + Whisper transcript, never re-run), makes sure
the vision features are there, then runs one iteration of the active P6-family prompt next to P5 with
fresh scoring (each run's score caches are cleared first, so every run is an independent sample), and
asks the adoption gate. The source session is only ever copied; its locked ground truth is not touched.
Accuracy comes from evaluate.compare_iteration inside each run (the only code that opens answer keys);
this script only reads the iteration records and the gate's answer.

Vision features. --vision shared (default): computed once, in run 001, and copied into every other
run, so the 100 samples measure the scoring prompts (P5 never sees vision features; per-run vision
noise would only blur P6). --vision per-child: every run computes its own (10 extra image calls each).

Each run is processed by a worker subprocess (config holds one open session per process), K at a
time. Progress goes to uploads/_collections/<id>/test_summary.json after every run, so the script can
be stopped and started again: finished runs are skipped, failed or interrupted ones are retried. A
usage limit that resets later than CLAUDE_CODE_MAX_WAIT_S stops new runs; start again after the reset.

    ../.venv/bin/python scripts/run_collection_test.py --children 100 --concurrency 2 --limit 2   # pilot
    ../.venv/bin/python scripts/run_collection_test.py --children 100 --concurrency 2           # all / resume
    ../.venv/bin/python scripts/run_collection_test.py --report-only
"""

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import collection, config  # noqa: E402

MARK = "@@RESULT@@ "
DEFAULT_SOURCE = "20261017-192847-808860"            # "XTB · 1", 10 real Reels, ground truth locked
DEFAULT_NAME = "Test 1 - XTB campaign"
WORKER_TIMEOUT_S = 3 * 3600


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(provider, model):
    from app import pipeline
    return pipeline.llm_slug({"provider": provider, "model": model})


# ---------------------------------------------------------------- worker (one run, own process)

def _open(child, provider, model):
    from app import dataset, ingest, pipeline
    dataset.open_session(child)
    dataset.set_llm({"provider": provider, "model": model})
    videos = list(ingest.load_inputs()[0])
    missing = [v for v in videos if pipeline.load(v, "preprocess") is None]
    if missing:                                       # never fall back to running Whisper here
        raise RuntimeError(f"no cached preprocess for {', '.join(missing)}: the copy is incomplete")
    return videos


def _features_phase(child, provider, model):
    from app import pipeline
    out = {}
    for vid in _open(child, provider, model):
        f = pipeline.analyse(vid)
        out[vid] = (f.get("_usage") or {})
    return {"status": "done", "features_usage": out}


def _run_phase(child, provider, model, prompt_id, vision):
    from app import iterations, jobs, pipeline
    from app import prompt_versions as pv
    videos = _open(child, provider, model)
    prompt_id = prompt_id or pv.active()
    choice = {"provider": provider, "model": model}
    for vid in videos:
        if pipeline.load(vid, "features") is None:
            if vision != "per-child":
                raise RuntimeError(f"{vid}: shared vision features were not copied into this run")
            pipeline.analyse(vid)
    pre = {v: (config.RUNS / v / "preprocess.json").stat().st_mtime_ns for v in videos}
    for vid in videos:                                # fresh scoring: this run's own answers only
        for st in (pv.stage(prompt_id), pv.stage(pv.BASELINE)):
            p = pipeline.stage_file(config.RUNS, vid, st, choice)
            if p is not None and "llm" in p.parts:
                p.unlink()
    t0 = time.perf_counter()
    snap = iterations.start(prompt_id, force=False,
                            note="run_collection_test.py: fresh P6-vs-P5 scoring (this run's score caches cleared)")
    if not snap:
        raise RuntimeError("another job is running in this process")
    n = snap["iteration"]
    while jobs.running():
        time.sleep(0.5)
    finish_error = jobs.snapshot().get("finish_error")
    rec = iterations.get(n)
    gate = iterations.adoption_check(prompt_id)
    calls = []
    for vid in videos:
        for kind, st in (("p6", pv.stage(prompt_id)), ("p5", pv.stage(pv.BASELINE))):
            u = (pipeline.load(vid, st) or {}).get("usage") or {}
            if u:
                calls.append({"video": vid, "kind": kind, **{k: u.get(k) for k in
                              ("latency_s", "input_tokens", "output_tokens", "cost_usd")}})
    errors = {v: x["error"] for v, x in (rec.get("videos") or {}).items() if x.get("error")}
    complete = rec["status"] == "done" and not errors and not finish_error and all(
        x.get("final") and x.get("p5_final") for x in rec["videos"].values())
    return {"status": "done" if complete else "error",
            "error": None if complete else (finish_error or "; ".join(f"{v}: {e}" for v, e in errors.items())
                                            or f"iteration status {rec['status']}"),
            "rate_limited": any("RateLimited" in (e or "") for e in errors.values()),
            "iteration": n, "prompt_id": prompt_id, "prompt_sha256": rec.get("prompt_sha256"),
            "scoring_s": round(time.perf_counter() - t0, 1),
            "videos": {v: {k: x.get(k) for k in ("final", "model_verdict", "score", "rule_override",
                                                 "p5_final", "p5_model", "p5_score", "error")}
                       for v, x in rec["videos"].items()},
            "comparison": _compact(rec.get("comparison")), "gate": gate, "calls": calls,
            "preprocess_rerun": [v for v in videos
                                 if (config.RUNS / v / "preprocess.json").stat().st_mtime_ns != pre[v]]}


def _compact(c):
    if not c or not c.get("basis"):
        return c
    keys = ("n", "tp", "fp", "fn", "tn", "accuracy", "recall", "precision", "winners")
    return {"basis": c["basis"], "beats_baseline": c["beats_baseline"], "why": c["why"],
            "p6": {k: c["candidate"][c["basis"]].get(k) for k in keys},
            "p5": {k: c["baseline"][c["basis"]].get(k) for k in keys}}


def worker(args):
    t0 = time.perf_counter()
    try:
        if args.phase == "features":
            out = _features_phase(args.worker, args.provider, args.model)
        else:
            out = _run_phase(args.worker, args.provider, args.model, args.prompt, args.vision)
    except Exception as e:                           # reported to the parent, never lost
        out = {"status": "error", "error": f"{type(e).__name__}: {e}", "rate_limited": type(e).__name__ == "RateLimited"}
    out["elapsed_s"] = round(time.perf_counter() - t0, 1)
    print(MARK + json.dumps(out, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------- parent (collection + scheduling)

def _spawn(child, args, phase, log):
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", child, "--phase", phase,
           "--provider", args.provider, "--model", args.model, "--vision", args.vision]
    if args.prompt:
        cmd += ["--prompt", args.prompt]
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=WORKER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"worker timed out after {WORKER_TIMEOUT_S}s", "rate_limited": False}
    log.write_text(f"$ {' '.join(cmd)}\n--- stdout\n{p.stdout}\n--- stderr\n{p.stderr}", encoding="utf-8")
    line = next((l for l in reversed(p.stdout.splitlines()) if l.startswith(MARK)), None)
    if line is None:
        return {"status": "error", "error": f"worker exited {p.returncode} without a result: {p.stderr.strip()[-300:]}",
                "rate_limited": False}
    return json.loads(line[len(MARK):])


class Summary:
    """test_summary.json under the collection folder; only the parent writes it, under a lock."""

    def __init__(self, folder, base):
        self.path, self.lock = folder / "test_summary.json", threading.Lock()
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else base

    def save(self):
        with self.lock:
            self.data["updated_at"] = _now()
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def set_child(self, child, **fields):
        with self.lock:
            self.data["children"].setdefault(child, {}).update(fields)
        self.save()


def _share_features(summary, folder, first, children, args):
    """Vision once, in the first run, then the same features.json copied into every run."""
    slug = _slug(args.provider, args.model)
    shared = folder / "shared_features" / slug
    from app import ingest
    config.activate(first)
    ingest.load_inputs.cache_clear()
    videos = list(ingest.load_inputs()[0])
    config.activate(None)
    if not all((shared / f"{v}.json").is_file() for v in videos):
        print(f"vision: computing shared features in {first} ({len(videos)} videos)…", flush=True)
        r = _spawn(first, args, "features", folder / "logs" / f"{first}.features.log")
        if r.get("status") != "done":
            raise SystemExit(f"shared vision step failed: {r.get('error')}")
        shared.mkdir(parents=True, exist_ok=True)
        for v in videos:
            shutil.copy2(config.UPLOADS / first / "runs" / v / "llm" / slug / "features.json", shared / f"{v}.json")
        with summary.lock:
            summary.data["shared_features"] = {"computed_in": first, "at": _now(), "elapsed_s": r.get("elapsed_s"),
                                               "usage": r.get("features_usage")}
        summary.save()
    for c in children:
        for v in videos:
            dst = config.UPLOADS / c / "runs" / v / "llm" / slug / "features.json"
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(shared / f"{v}.json", dst)


def run(args):
    rec = collection.find(args.name, args.source)
    if rec is None:
        print(f"creating collection {args.name!r} from {args.source}", flush=True)
        rec = collection.create(args.source, args.name)
    cid = rec["id"]
    have = len(collection.get(cid)["child_ids"])
    if have < args.children:
        print(f"copying {args.children - have} runs of {args.source} (APFS clones on macOS)…", flush=True)
        collection.add_children(cid, args.children - have)
    children = collection.get(cid)["child_ids"][:args.children]
    folder = collection.folder(cid)
    llm = {"provider": args.provider, "model": args.model}
    summary = Summary(folder, {"collection": cid, "name": args.name, "source": args.source, "llm": llm,
                               "vision": args.vision, "created_at": _now(), "children": {}})
    if summary.data.get("llm") != llm or summary.data.get("vision") != args.vision:
        raise SystemExit(f"this collection's test ran on {summary.data.get('llm')} with vision "
                         f"{summary.data.get('vision')}; use another --name for a different setup")
    summary.save()

    todo = [c for c in children if (summary.data["children"].get(c) or {}).get("status") != "done"
            and not (args.skip_errors and (summary.data["children"].get(c) or {}).get("status") == "error")]
    if args.limit is not None:
        todo = todo[:args.limit]
    print(f"{cid}: {len(children)} runs, {len(children) - len(todo)} skipped, {len(todo)} to run, "
          f"concurrency {args.concurrency}", flush=True)
    if todo and args.vision == "shared":
        _share_features(summary, folder, children[0], children, args)

    stop = threading.Event()

    def one(child):
        if stop.is_set():
            return
        index = children.index(child) + 1
        summary.set_child(child, index=index, status="running", started_at=_now(), error=None)
        r = _spawn(child, args, "run", folder / "logs" / f"{child}.log")
        summary.set_child(child, finished_at=_now(), **r)
        print(f"  run {index:03d} {child}: {r['status']} in {r.get('elapsed_s')}s"
              + (f" - {r['error'][:200]}" if r.get("error") else ""), flush=True)
        if r.get("rate_limited"):
            stop.set()
            print("  usage / rate limit reached: no new runs start; run the script again after the reset", flush=True)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        list(ex.map(one, todo))
    with summary.lock:
        summary.data.setdefault("batches", []).append({"at": _now(), "runs": len(todo), "concurrency": args.concurrency,
                                                       "elapsed_s": round(time.perf_counter() - t0, 1),
                                                       "stopped_on_limit": stop.is_set()})
    summary.save()
    report(folder, summary.data)


# ---------------------------------------------------------------- report

def _stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0, "mean": None, "stdev": None, "min": None, "max": None}
    return {"n": len(xs), "mean": round(statistics.fmean(xs), 3),
            "stdev": round(statistics.stdev(xs), 3) if len(xs) > 1 else None,
            "min": round(min(xs), 3), "max": round(max(xs), 3)}


def _pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 2) if xs else None


def build_report(data):
    kids = data.get("children") or {}
    done = {c: k for c, k in kids.items() if k.get("status") == "done"}
    per_video = {}
    for k in done.values():
        for v, x in (k.get("videos") or {}).items():
            pv_ = per_video.setdefault(v, {"p6": Counter(), "p6_model": Counter(), "p5": Counter(), "p6_score": [], "p5_score": []})
            pv_["p6"][x.get("final")] += 1
            pv_["p6_model"][x.get("model_verdict")] += 1
            pv_["p5"][x.get("p5_final")] += 1
            pv_["p6_score"].append(x.get("score"))
            pv_["p5_score"].append(x.get("p5_score"))
    videos = {}
    for v, x in sorted(per_video.items()):
        n = sum(x["p6"].values())
        videos[v] = {"p6": dict(x["p6"]), "p6_model": dict(x["p6_model"]), "p5": dict(x["p5"]),
                     "p6_modal_share": round(x["p6"].most_common(1)[0][1] / n, 3) if n else None,
                     "p5_modal_share": round(x["p5"].most_common(1)[0][1] / n, 3) if n else None,
                     "p6_score": _stats(x["p6_score"]), "p5_score": _stats(x["p5_score"])}
    comps = [k.get("comparison") or {} for k in done.values()]
    scored = [c for c in comps if c.get("basis")]

    def agree(k, scale_only):
        xs = [(x.get("final"), x.get("p5_final")) for x in (k.get("videos") or {}).values()]
        if scale_only:
            xs = [(a == "SCALE", b == "SCALE") for a, b in xs]
        return sum(a == b for a, b in xs) / len(xs) if xs else None

    calls = [c for k in done.values() for c in k.get("calls") or []]
    lat = {kind: [c["latency_s"] for c in calls if c["kind"] == kind] for kind in ("p6", "p5")}
    costs = [c.get("cost_usd") for c in calls if c.get("cost_usd") is not None]
    gates = [bool((k.get("gate") or {}).get("beats_baseline")) for k in done.values()]
    shared = data.get("shared_features") or {}
    return {
        "collection": data.get("collection"), "name": data.get("name"), "source": data.get("source"),
        "llm": data.get("llm"), "vision": data.get("vision"), "generated_at": _now(),
        "runs": {"planned": len(kids), "done": len(done),
                 "error": sum(1 for k in kids.values() if k.get("status") == "error"),
                 "running_or_interrupted": sum(1 for k in kids.values() if k.get("status") == "running")},
        "basis": scored[0]["basis"] if scored else None,
        "accuracy": {"p6": _stats([c["p6"]["accuracy"] for c in scored]),
                     "p5": _stats([c["p5"]["accuracy"] for c in scored])},
        "false_scale": {"p6": _stats([c["p6"]["fp"] for c in scored]), "p5": _stats([c["p5"]["fp"] for c in scored]),
                        "p6_runs_with_any": sum(1 for c in scored if c["p6"]["fp"]),
                        "p5_runs_with_any": sum(1 for c in scored if c["p5"]["fp"])},
        "winners_caught": {"p6": _stats([c["p6"]["tp"] for c in scored]), "p5": _stats([c["p5"]["tp"] for c in scored]),
                           "winners": scored[0]["p6"]["winners"] if scored else None},
        "agreement_p5_p6": {"exact_verdict": _stats([agree(k, False) for k in done.values()]),
                            "scale_call": _stats([agree(k, True) for k in done.values()])},
        "gate_pass_rate": round(sum(gates) / len(gates), 3) if gates else None,
        "per_video": videos,
        "timing": {"run_s": _stats([k.get("elapsed_s") for k in done.values()]),
                   "call_s": {kind: {**_stats(xs), "p50": _pct(xs, 0.5), "p90": _pct(xs, 0.9)} for kind, xs in lat.items()},
                   "calls": len(calls), "shared_vision_s": shared.get("elapsed_s")},
        "cost_usd_list_price": {"total": round(sum(costs), 4) if costs else None,
                                "per_run": round(sum(costs) / len(done), 4) if costs and done else None,
                                "note": "what the calls would cost at API list price; a subscription is not billed per call"},
        "preprocess_rerun": sorted({v for k in done.values() for v in k.get("preprocess_rerun") or []}),
        "failures": [{"child": c, "index": k.get("index"), "error": k.get("error")}
                     for c, k in kids.items() if k.get("status") in ("error", "running")],
    }


def _fmt(s):
    if not s or not s.get("n"):
        return "n/a"
    return f"{s['mean']} ± {s['stdev'] if s['stdev'] is not None else '-'} (min {s['min']}, max {s['max']}, n={s['n']})"


def render(r):
    t = r["timing"]
    lines = [f"# {r['name']} - {r['llm']['provider']} / {r['llm']['model']} (vision {r['vision']})",
             f"runs: {r['runs']['done']} done, {r['runs']['error']} failed, {r['runs']['running_or_interrupted']} "
             f"interrupted, {r['runs']['planned']} planned · basis: {r['basis']}",
             f"accuracy  P6 {_fmt(r['accuracy']['p6'])}",
             f"          P5 {_fmt(r['accuracy']['p5'])}",
             f"false SCALE  P6 {_fmt(r['false_scale']['p6'])} · runs with any: {r['false_scale']['p6_runs_with_any']}",
             f"             P5 {_fmt(r['false_scale']['p5'])} · runs with any: {r['false_scale']['p5_runs_with_any']}",
             f"winners caught (of {r['winners_caught']['winners']})  P6 {_fmt(r['winners_caught']['p6'])} · "
             f"P5 {_fmt(r['winners_caught']['p5'])}",
             f"P5/P6 agreement  exact {_fmt(r['agreement_p5_p6']['exact_verdict'])} · "
             f"scale call {_fmt(r['agreement_p5_p6']['scale_call'])}",
             f"gate pass rate (P6 beats P5): {r['gate_pass_rate']}",
             f"time per run {_fmt(t['run_s'])} s · calls {t['calls']} · P6 call {t['call_s']['p6'].get('p50')} s p50 / "
             f"{t['call_s']['p6'].get('p90')} s p90 · P5 call {t['call_s']['p5'].get('p50')} s p50 / "
             f"{t['call_s']['p5'].get('p90')} s p90 · shared vision {t['shared_vision_s']} s",
             f"list-price cost: {r['cost_usd_list_price']['total']} USD total, {r['cost_usd_list_price']['per_run']} per run",
             "", "| video | P6 final verdicts | P6 model verdicts | P5 final verdicts | P6 score | stable P6 / P5 |",
             "|---|---|---|---|---|---|"]
    for v, x in r["per_video"].items():
        fmt = lambda c: ", ".join(f"{k} {n}" for k, n in sorted(c.items(), key=lambda kv: -kv[1]))
        lines.append(f"| {v} | {fmt(x['p6'])} | {fmt(x['p6_model'])} | {fmt(x['p5'])} | {x['p6_score']['mean']} ± "
                     f"{x['p6_score']['stdev']} | {x['p6_modal_share']} / {x['p5_modal_share']} |")
    if r["preprocess_rerun"]:
        lines.append(f"\nWARNING: preprocess changed during a run for {', '.join(r['preprocess_rerun'])}")
    if r["failures"]:
        lines += ["", "failures:"] + [f"- run {f['index']} {f['child']}: {f['error']}" for f in r["failures"]]
    return "\n".join(lines) + "\n"


def report(folder, data):
    r = build_report(data)
    (folder / "report.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    text = render(r)
    (folder / "report.md").write_text(text, encoding="utf-8")
    print("\n" + text + f"\nsaved {folder / 'report.json'} and report.md", flush=True)
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=DEFAULT_SOURCE, help="session id to copy (only read)")
    ap.add_argument("--name", default=DEFAULT_NAME, help="collection name; an existing one is resumed")
    ap.add_argument("--children", type=int, default=100, help="runs in the collection (made as needed)")
    ap.add_argument("--concurrency", type=int, default=2, help="runs at a time (mind subscription limits)")
    ap.add_argument("--limit", type=int, default=None, help="run at most this many runs now (pilot)")
    ap.add_argument("--provider", default="claude_code", choices=list(config.LLM_PROVIDERS))
    ap.add_argument("--model", default=None, help="default: the provider's default model")
    ap.add_argument("--vision", default="shared", choices=["shared", "per-child"])
    ap.add_argument("--prompt", default=None, help="P6-family prompt id; default: each run's active prompt")
    ap.add_argument("--skip-errors", action="store_true", help="do not retry runs that failed before")
    ap.add_argument("--report-only", action="store_true", help="rebuild the report from the summary, run nothing")
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    ap.add_argument("--phase", default="run", choices=["run", "features"], help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    args.model = args.model or config.DEFAULT_MODELS.get(args.provider)
    if args.worker:
        return worker(args)
    if args.report_only:
        rec = collection.find(args.name, args.source)
        if rec is None:
            raise SystemExit(f"no collection named {args.name!r}")
        folder = collection.folder(rec["id"])
        return report(folder, json.loads((folder / "test_summary.json").read_text(encoding="utf-8")))
    return run(args)


if __name__ == "__main__":
    main()
