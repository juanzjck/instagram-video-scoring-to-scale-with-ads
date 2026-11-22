"""Score-only collection test: N samples of one P6-family prompt on one campaign, without session copies.

run_collection_test.py copies the whole session per run (~108 MB) and scores P6.x and P5 one after the
other (20 calls per run). When only the prompt changes, everything but the scoring call is the same in
every run, so this script reads the source session once and makes N x 10 P6.x calls, `--concurrency` at a
time:

- The prompt input is the pipeline's own: pipeline.summary() (flatten() + `=== CONTENT FEATURES ===` from
  the source's cached features.json for this provider + model; the injection guard redacts inside
  flatten()), scored by scoring.score() (all calls through app/llm.py, rule guard applied in code after the
  model answers, the model's verdict kept alongside). No vision call is made: missing features stop it.
- Answers are appended to uploads/_collections/<id>/answers.jsonl (one line per call). Starting again with
  the same --name resumes: done (run, video) pairs are skipped, failed ones retried. A usage limit / 429
  (or a provider that cannot run) stops new calls at once; what is written stays.
- P5 is not re-scored by default: --p5-from <collection id> reuses the P5 verdicts of a finished
  run_collection_test.py collection of the same source (paired with these runs by index, cycled or
  truncated when the counts differ), or --with-p5 scores P5 fresh the same way.
- report.json / report.md come from run_collection_test.build_report / render; accuracy per run comes from
  evaluate.compare_iteration against the source's locked ground truth (the only code that opens answer keys).
The source session is only read; nothing under it is written.

    .venv/bin/python scripts/score_only.py --source 20261119-152838-287693 --prompt P6.3 \\
        --p5-from col-20261119-180338-923160 --runs 30 --concurrency 4
    .venv/bin/python scripts/score_only.py --source ... --prompt P6.3 --p5-from ... --report-only
"""

import argparse
import hashlib
import importlib.util
import json
import threading
import time
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import collection, config, evaluate, guard, ingest, llm, pipeline, scoring  # noqa: E402
from app import prompt_versions as pv  # noqa: E402

_spec = importlib.util.spec_from_file_location("run_collection_test", Path(__file__).resolve().parent / "run_collection_test.py")
rct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rct)

META, ANSWERS = "score_only.json", "answers.jsonl"
STOP_ERRORS = ("RateLimited", "RateLimitError", "ProviderUnavailable")   # every further call would fail too


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(p, data):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------- inputs (read once from the source)

def _open_source(source):
    try:
        config.activate(source)
    except ValueError as e:
        raise SystemExit(f"--source: {e}")
    ingest.load_inputs.cache_clear()
    return list(ingest.load_inputs()[0])


def _prompt(prompt_id):
    if prompt_id == pv.BASELINE:
        raise SystemExit("--prompt: P5 is the baseline; use --with-p5 or --p5-from for it and pick a P6-family version")
    try:
        v = pv.get(prompt_id)
    except KeyError:
        raise SystemExit(f"--prompt: unknown prompt version {prompt_id!r}")
    if not v.get("multimodal"):
        raise SystemExit(f"--prompt: {prompt_id} is not a P6-family prompt")
    return v


def build_inputs(videos, choice, with_p5):
    """Per video: the row (for the rule guard), the exact P6 (and P5) prompt input, and the guard's flags."""
    missing = [v for v in videos if pipeline.load(v, "preprocess") is None or pipeline.load(v, "features", choice) is None]
    if missing:
        raise SystemExit(f"no cached preprocess / vision features for {choice['provider']} / {choice['model']} in the "
                         f"source for {', '.join(missing)}: run the pipeline (or run_collection_test.py) on it first")
    out = {}
    for vid in videos:
        row, comments = pipeline._row(vid)
        pre, feats = pipeline.load(vid, "preprocess"), pipeline.load(vid, "features", choice)
        texts = [] if comments is None else [str(t) for t in comments["text"].tolist()]
        out[vid] = {"row": row, "p6": pipeline.summary(vid, True, choice=choice),
                    "p5": pipeline.summary(vid, False) if with_p5 else None,
                    "injection_flags": len(guard.find_flags(pre.get("transcript"), feats.get("frames") or pre.get("frames"), texts))}
    return out


# ---------------------------------------------------------------- answers.jsonl

def read_answers(folder):
    p = folder / ANSWERS
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue                                  # a line cut by a crash: that call is simply redone
    return out


def latest(answers):
    """(run, kind, video) -> its done record when there is one, else its last attempt."""
    out = {}
    for a in answers:
        k = (a["run"], a["kind"], a["video"])
        if out.get(k, {}).get("status") != "done":
            out[k] = a
    return out


def _call(kind, prompt_id, v, inp, choice):
    if kind == "p6":
        return scoring.score(prompt_id, inp["p6"], inp["row"], template=None if v["builtin"] else v["text"], choice=choice)
    return scoring.score(pv.BASELINE, inp["p5"], inp["row"], choice=choice)


def score_all(folder, tasks, inputs, prompt_id, v, choice, concurrency):
    """Run (run, kind, video) tasks, `concurrency` at a time, appending one line per call. Returns
    (calls made, stopped on a limit)."""
    lock, stop, made = threading.Lock(), threading.Event(), [0]
    path = folder / ANSWERS
    if path.is_file() and path.stat().st_size and not path.read_bytes().endswith(b"\n"):
        with path.open("a", encoding="utf-8") as f:   # a line cut by a crash must not swallow the next record
            f.write("\n")

    def one(task):
        if stop.is_set():
            return
        run, kind, vid = task
        rec = {"run": run, "kind": kind, "video": vid, "at": _now(),
               "prompt_id": prompt_id if kind == "p6" else pv.BASELINE,
               "input_sha256": _sha(inputs[vid][kind]), "injection_flags": inputs[vid]["injection_flags"]}
        try:
            out = _call(kind, prompt_id, v, inputs[vid], choice)
            rec.update(status="done", error=None, rate_limited=False,
                       **{k: out.get(k) for k in ("verdict", "model_verdict", "score", "score_low", "score_high",
                                                  "confidence", "rule_override", "rule_hits", "reasons",
                                                  "prompt_sha256", "usage", "raw_output")})
        except Exception as e:                        # recorded, retried on the next start
            name = type(e).__name__
            rec.update(status="error", error=f"{name}: {e}", rate_limited=name in STOP_ERRORS)
            if rec["rate_limited"]:
                stop.set()
        with lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            made[0] += 1
        print(f"  run {run:03d} {kind} {vid}: {rec.get('verdict') or rec['status']}"
              + (f" - {rec['error'][:200]}" if rec["error"] else ""), flush=True)
        if rec["rate_limited"]:
            print("  usage / rate limit (or provider unavailable): no new calls start; run again after the reset",
                  flush=True)

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        list(ex.map(one, tasks))
    return made[0], stop.is_set()


# ---------------------------------------------------------------- P5 samples

def p5_samples_from(cid, source, videos):
    """The P5 verdicts of every finished run of a run_collection_test.py collection, in run order.
    Refused unless every sample covers every source video (a run missing one could never finish)."""
    try:
        p = collection.folder(cid) / "test_summary.json"
    except KeyError:
        raise SystemExit(f"--p5-from: unknown collection {cid!r}")
    if not p.is_file():
        raise SystemExit(f"--p5-from: {cid} has no test_summary.json (not a run_collection_test.py collection)")
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("source") != source:
        raise SystemExit(f"--p5-from: {cid} ran on source {data.get('source')}, not {source}")
    done = sorted((k for k in (data.get("children") or {}).values() if k.get("status") == "done"),
                  key=lambda k: k.get("index") or 0)
    samples = [{v: {f: x.get(f) for f in ("p5_final", "p5_model", "p5_score")} for v, x in (k.get("videos") or {}).items()}
               for k in done]
    samples = [s for s in samples if s and all(x["p5_final"] for x in s.values())]
    if not samples:
        raise SystemExit(f"--p5-from: {cid} has no finished run with P5 verdicts")
    missing = sorted({v for s in samples for v in videos if not (s.get(v) or {}).get("p5_final")})
    if missing:
        raise SystemExit(f"--p5-from: {cid} has no P5 verdict for source video(s) {', '.join(missing)}")
    return samples, data.get("llm")


def _llm_warning(p5_llm, choice):
    p5_llm = p5_llm or {}
    return (f"P5 samples come from {p5_llm.get('provider')} / {p5_llm.get('model')}, P6 runs on "
            f"{choice['provider']} / {choice['model']}: the P5 / P6 comparison mixes models")


def pairing_note(n_runs, n_samples):
    if n_samples == n_runs:
        return f"1:1 ({n_runs} P5 samples for {n_runs} runs)"
    if n_samples > n_runs:
        return f"truncated: the first {n_runs} of {n_samples} P5 samples"
    return f"cycled: {n_samples} P5 samples reused for {n_runs} runs (run i gets sample (i-1) mod {n_samples})"


# ---------------------------------------------------------------- report

def children(answers, videos, n_runs, p5_samples=None):
    """Per run, a record shaped like a run_collection_test.py test_summary child, so its build_report reads it."""
    got = latest(answers)
    out = {}
    for run in range(1, n_runs + 1):
        vids, calls, errors = {}, [], []
        for vid in videos:
            a6 = got.get((run, "p6", vid)) or {}
            p5 = None
            if p5_samples:
                x = p5_samples[(run - 1) % len(p5_samples)].get(vid) or {}
                p5 = {f: x.get(f) for f in ("p5_final", "p5_model", "p5_score")}
            if p5 is None:
                a5 = got.get((run, "p5", vid)) or {}
                p5 = {"p5_final": a5.get("verdict") if a5.get("status") == "done" else None,
                      "p5_model": a5.get("model_verdict"), "p5_score": a5.get("score")}
                if a5.get("status") == "error":
                    errors.append(f"{vid} p5: {a5['error']}")
            ok6 = a6.get("status") == "done"
            if a6.get("status") == "error":
                errors.append(f"{vid}: {a6['error']}")
            vids[vid] = {"final": a6.get("verdict") if ok6 else None, "model_verdict": a6.get("model_verdict"),
                         "score": a6.get("score"), "rule_override": a6.get("rule_override"), **p5,
                         "error": a6.get("error")}
            for kind, a in (("p6", a6), ("p5", None if p5_samples else got.get((run, "p5", vid)))):
                if a and a.get("status") == "done":
                    u = a.get("usage") or {}
                    calls.append({"video": vid, "kind": kind, **{k: u.get(k) for k in
                                                                 ("latency_s", "input_tokens", "output_tokens", "cost_usd")}})
        complete = all(x["final"] and x["p5_final"] for x in vids.values())
        touched = any((run, k, v) in got for k in ("p6", "p5") for v in videos)
        rec = {"index": run, "status": "done" if complete else "error" if errors else "running" if touched else "pending",
               "error": "; ".join(errors) or None, "videos": vids, "calls": calls, "preprocess_rerun": [],
               # calls run in parallel: a run's time is the sum of its call latencies (what it would take one by one)
               "elapsed_s": round(sum(c["latency_s"] or 0 for c in calls), 1) if complete else None}
        if complete:
            c = evaluate.compare_iteration(vids)
            rec["comparison"] = rct._compact(c)
            rec["gate"] = {"beats_baseline": c["beats_baseline"], "why": c["why"], "iteration": None}
        out[f"run-{run:03d}"] = rec
    return out


def report(folder, meta, videos):
    answers = read_answers(folder)
    p5 = meta["p5"]
    samples = p5_samples_from(p5["from"], meta["source"], videos)[0] if p5["mode"] == "from" else None
    data = {"collection": meta["collection"], "name": meta["name"], "source": meta["source"], "llm": meta["llm"],
            "vision": "source features (score-only)", "children": children(answers, videos, meta["runs"], samples)}
    r = rct.build_report(data)
    batches = meta.get("batches") or []
    r["score_only"] = {
        "prompt_id": meta["prompt_id"], "prompt_sha256": meta["prompt_sha256"], "answers": len(answers),
        "p5": dict(p5, samples=len(samples) if samples else None,
                   pairing=pairing_note(meta["runs"], len(samples)) if samples else "fresh, same run index"),
        "wall_s": round(sum(b["elapsed_s"] for b in batches), 1), "batches": batches,
        "run_s_note": "time per run = sum of its call latencies; calls ran in parallel (see wall_s)",
        "warning": _llm_warning(p5.get("llm"), meta["llm"]) if p5["mode"] == "from" and p5.get("llm") != meta["llm"] else None}
    (folder / "report.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    s = r["score_only"]
    extra = (f"\nscore-only: {s['prompt_id']} ({s['prompt_sha256'][:12]}) · P5 "
             + (f"from {p5['from']} ({(p5.get('llm') or {}).get('provider')} / {(p5.get('llm') or {}).get('model')}), "
                f"{s['p5']['pairing']}" if p5["mode"] == "from" else "scored fresh")
             + f" · {s['answers']} answers · wall clock {s['wall_s']} s over {len(batches)} batches · {s['run_s_note']}\n")
    text = rct.render(r) + extra + (f"WARNING: {s['warning']}\n" if s["warning"] else "")
    (folder / "report.md").write_text(text, encoding="utf-8")
    print("\n" + text + f"\nsaved {folder / 'report.json'} and report.md", flush=True)
    return r


# ---------------------------------------------------------------- main

def _setup(args):
    choice = {"provider": args.provider, "model": args.model}
    v = _prompt(args.prompt)
    videos = _open_source(args.source)
    if not videos:
        raise SystemExit(f"--source: {args.source} has no videos")
    p5 = {"mode": "fresh"} if args.with_p5 else {"mode": "from", "from": args.p5_from}
    if p5["mode"] == "from":
        p5["llm"] = p5_samples_from(args.p5_from, args.source, videos)[1]
        if p5["llm"] != choice:
            print(f"WARNING: {_llm_warning(p5['llm'], choice)}", flush=True)
    name = args.name or f"Score-only {args.prompt} · {args.provider} / {args.model}"
    return choice, v, videos, p5, name


def run(args):
    choice, v, videos, p5, name = _setup(args)
    inputs = build_inputs(videos, choice, args.with_p5)
    rec = collection.find(name, args.source)
    if rec is not None and not (collection.folder(rec["id"]) / META).is_file() and (
            (collection.folder(rec["id"]) / "test_summary.json").is_file() or collection.run_ids(rec)):
        raise SystemExit(f"collection {name!r} ({rec['id']}) is not a score-only collection (it has runs or a "
                         "run_collection_test.py summary); use another --name")
    if rec is None:
        print(f"creating collection {name!r} of {args.source} (no session copies)", flush=True)
        rec = collection.create(args.source, name)
    folder = collection.folder(rec["id"])
    mp = folder / META
    fixed = {"prompt_id": args.prompt, "prompt_sha256": v["sha256"], "llm": choice, "p5": p5,
             "inputs": {vid: _sha(x["p6"]) for vid, x in inputs.items()}}
    meta = json.loads(mp.read_text(encoding="utf-8")) if mp.is_file() else {
        "collection": rec["id"], "name": name, "source": args.source, "created_at": _now(), **fixed}
    diff = [k for k in fixed if meta.get(k) != fixed[k]]
    if diff:
        raise SystemExit(f"collection {name!r} was scored with another {', '.join(diff)}; use another --name")
    meta["runs"] = max(meta.get("runs") or 0, args.runs)
    _write_json(mp, meta)

    got = latest(read_answers(folder))
    kinds = ("p6", "p5") if args.with_p5 else ("p6",)
    todo = [(r, k, vid) for r in range(1, meta["runs"] + 1) for vid in videos for k in kinds
            if (got.get((r, k, vid)) or {}).get("status") != "done"]
    print(f"{rec['id']}: {meta['runs']} runs x {len(videos)} videos, {len(todo)} calls to make, "
          f"concurrency {args.concurrency}", flush=True)
    t0 = time.perf_counter()
    made, stopped = score_all(folder, todo, inputs, args.prompt, v, choice, args.concurrency)
    meta.setdefault("batches", []).append({"at": _now(), "calls": made, "planned": len(todo),
                                           "concurrency": args.concurrency,
                                           "elapsed_s": round(time.perf_counter() - t0, 1), "stopped_on_limit": stopped})
    _write_json(mp, meta)
    return report(folder, meta, videos)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="session id to score (only read)")
    ap.add_argument("--prompt", required=True, help="P6-family prompt id (P6, P6.2, ...); P5 is refused")
    ap.add_argument("--runs", type=int, default=30, help="samples per video (grows an existing collection)")
    ap.add_argument("--concurrency", type=int, default=4, help="calls at a time (mind subscription limits)")
    ap.add_argument("--provider", default="claude_code", choices=list(config.LLM_PROVIDERS))
    ap.add_argument("--model", default=None, help="default: the provider's default model")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--p5-from", help="reuse P5 verdicts from this run_collection_test.py collection id")
    g.add_argument("--with-p5", action="store_true", help="score P5 fresh, same runs")
    ap.add_argument("--name", default=None, help="collection name; an existing one is resumed")
    ap.add_argument("--report-only", action="store_true", help="rebuild the report from answers.jsonl, call nothing")
    args = ap.parse_args(argv)
    args.model = args.model or config.DEFAULT_MODELS.get(args.provider)
    if args.runs < 1:
        ap.error("--runs must be at least 1")
    if args.report_only:
        _, _, videos, _, name = _setup(args)
        rec = collection.find(name, args.source)
        if rec is None or not (collection.folder(rec["id"]) / META).is_file():
            raise SystemExit(f"no score-only collection named {name!r} for {args.source}")
        folder = collection.folder(rec["id"])
        return report(folder, json.loads((folder / META).read_text(encoding="utf-8")), videos)
    return run(args)


if __name__ == "__main__":
    main()
