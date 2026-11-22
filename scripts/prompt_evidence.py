"""Evidence pack for the next prompt version: every scored answer in every session and collection, per prompt.

Read only, no model call. Scans uploads/:

- every session's score caches (runs/<video>/llm/<provider>--<model>/score_*.json, and the pre-model-folder
  runs/<video>/score_*.json), its decision log (runs/decisions.jsonl), blind reviews (manual_review.csv) and
  adoption log (session.json "prompts");
- every collection in uploads/_collections/: run_collection_test.py ones (test_summary.json, report.json; a run
  whose session folder is gone still counts, verdicts only) and score_only.py ones (answers.jsonl, report.json).

Copies made by dataset.copy_session carry the source's cache files byte for byte; a file seen twice is one
sample. Answers are grouped by prompt id + text SHA-256 (an id whose cached text differs from today's library
text is shown as "P6.2@<sha8>") and by source campaign (a collection run counts under its collection's source).
Accuracy, false SCALE and winners caught per run come from evaluate.truth / evaluate.verdict_metrics (the only
code that opens answer keys); the gate ranking uses evaluate.beats. Missing scores stay None, never 0.

Writes evidence.json, evidence.md and prompts/<id>.txt to --out (default: a new folder under the system temp
dir), never under uploads/. Model reasons and reviewer notes are untrusted text (they may quote comments or
transcripts): evidence.md keeps them inside fenced "UNTRUSTED" blocks.

    .venv/bin/python scripts/prompt_evidence.py --out /tmp/evidence [--source <session id>] [--prompts P6,P6.2] [--samples 3]
"""

import argparse
import csv
import difflib
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import collection, config, evaluate, guard, pipeline  # noqa: E402
from app.ingest import num  # noqa: E402
from app import prompt_versions as pv  # noqa: E402


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rct = _load_script("run_collection_test")
so = _load_script("score_only")

FREQUENT_WRONG = 0.2      # a video is listed when its modal verdict is wrong or this share of its answers is
SMALL = {"videos": 20, "sources": 2, "runs": 5}     # below these, a gain is likely to be noise or overfitting


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _minutes(x):
    try:
        return num(x)
    except ValueError:
        return None


def _scale(v):
    return None if v is None else ("SCALE" if v == "SCALE" else "DO_NOT_SCALE")


# ---------------------------------------------------------------- prompt versions (read only)

def versions():
    """Every version with its text: built-ins, then the library. Read straight from library.json, so the
    one-time migration in prompt_versions._lib() (which writes) never runs from here."""
    lib = pv._lib_read()
    out = [pv._builtin(pv.BASELINE), pv._builtin(pv.DEFAULT_ACTIVE)]
    out += [dict(v, builtin=False, multimodal=True) for v in lib.get("versions") or []]
    return out, bool(lib.get("migrated"))


def _diff(v, by_id, migrated):
    if not v.get("parent") or v["parent"] not in by_id:
        return None
    if migrated:
        return pv.diff(v["id"])
    base = by_id[v["parent"]]
    return "".join(difflib.unified_diff(base["text"].splitlines(keepends=True), v["text"].splitlines(keepends=True),
                                        fromfile=base["id"], tofile=v["id"])) or "(no differences)\n"


# ---------------------------------------------------------------- collecting samples

def _sample(pid, sha, slug, source, sid, video, run, origin, x, reasons=True):
    return {"prompt_id": pid, "prompt_sha256": sha, "llm": slug, "source": source, "session": sid, "video": video,
            "run": run, "origin": origin, "final": x.get("verdict"), "model_verdict": x.get("model_verdict"),
            "score": _num(x.get("score")), "confidence": x.get("confidence"),
            "rule_override": x.get("rule_override"), "reasons": (x.get("reasons") or None) if reasons else None}


def _cache_files(folder):
    runs = folder / "runs"
    if not runs.is_dir():
        return
    for vdir in sorted(p for p in runs.iterdir() if p.is_dir() and p.name != "iterations"):
        for p in sorted(vdir.glob("score_*.json")):
            yield vdir.name, None, p
        for p in sorted(vdir.glob("llm/*/score_*.json")):
            yield vdir.name, p.parent.name, p


def _legacy_slug(x):
    u = x.get("usage") or {}
    return pipeline.llm_slug({"provider": u.get("provider") or "anthropic", "model": u.get("model") or "unknown"})


def scan(uploads):
    """Everything the evidence is built from: samples, human decisions, adoptions, collections, sessions."""
    cols = {c["id"]: c for c in collection._all()}
    sessions, samples, seen_files, seen = {}, [], set(), set()
    humans = defaultdict(dict)                    # (source, video) -> {dedupe key: decision}
    adoptions = {}
    for folder in pv._sessions():
        s = _json(folder / "session.json")
        if not isinstance(s, dict):
            continue
        sid = folder.name
        mark = s.get("collection") or {}
        source = (cols.get(mark.get("id")) or {}).get("source") or sid
        sessions[sid] = {"id": sid, "name": s.get("name"), "source": source, "collection": mark.get("id"),
                         "run": s.get("run"), "active": (s.get("prompts") or {}).get("active")}
        for a in (s.get("prompts") or {}).get("adoptions") or []:
            adoptions[json.dumps(a, sort_keys=True, ensure_ascii=False)] = dict(a, session=sid, source=source)
        for video, slug, p in _cache_files(folder):
            x = _json(p)
            if not isinstance(x, dict) or not x.get("verdict"):
                continue
            slug = slug or _legacy_slug(x)
            pid = x.get("prompt_id") or p.stem.removeprefix("score_").upper()
            seen.add((sid, slug, pid, video))      # before the copy check: test_summary must not re-add a copy
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            if digest in seen_files:               # a copy of an answer already counted
                continue
            seen_files.add(digest)
            samples.append(_sample(pid, x.get("prompt_sha256"), slug, source, sid, video, f"{sid}|{slug}",
                                   str(p.relative_to(uploads)), x))
        for line in ((folder / "runs" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
                     if (folder / "runs" / "decisions.jsonl").is_file() else []):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            h = {"kind": "card decision", "reviewer": d.get("reviewer"), "decision": d.get("human_verdict"),
                 "action": d.get("action"), "ai_verdict": d.get("ai_verdict"), "note": d.get("note") or None,
                 "at": d.get("ts")}
            humans[(source, d.get("video_id"))].setdefault(json.dumps(h, sort_keys=True, ensure_ascii=False), h)
        mr = folder / "manual_review.csv"
        if mr.is_file():
            with mr.open(encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    h = {"kind": "blind review", "reviewer": r.get("reviewer") or None, "decision": r.get("decision") or None,
                         "note": r.get("reason") or None,
                         "minutes": _minutes(r.get("minutes_spent"))}
                    humans[(source, r.get("video_id"))].setdefault(json.dumps(h, sort_keys=True, ensure_ascii=False), h)

    out_cols = []
    for cid, rec in sorted(cols.items()):
        folder = collection.root() / cid
        meta, summary = _json(folder / so.META), _json(folder / "test_summary.json")
        style = "score_only" if meta else "collection_test" if summary else "app_runs"
        report = _json(folder / "report.json")
        entry = {"id": cid, "name": rec.get("name"), "source": rec.get("source"), "style": style,
                 "runs": len(collection.run_ids(rec)), "llm": (meta or summary or {}).get("llm"), "prompts": [],
                 "report": None}
        if style == "score_only":
            entry["prompts"] = [meta.get("prompt_id")]
            entry["p5"] = meta.get("p5")
            slug = pipeline.llm_slug(meta["llm"]) if meta.get("llm") else "unknown"
            for i, a in enumerate(so.read_answers(folder)):
                if a.get("status") != "done" or not a.get("verdict"):
                    continue
                pid = a.get("prompt_id") or (meta.get("prompt_id") if a.get("kind") == "p6" else pv.BASELINE)
                sha = a.get("prompt_sha256") or (meta.get("prompt_sha256") if pid == meta.get("prompt_id") else None)
                samples.append(_sample(pid, sha, slug, rec["source"], None, a["video"], f"{cid}#run-{a['run']:03d}",
                                       f"_collections/{cid}/{so.ANSWERS}:{i + 1}", a))
        elif style == "collection_test":
            slug = pipeline.llm_slug(summary["llm"]) if summary.get("llm") else "unknown"
            p5_sha = pv._builtin(pv.BASELINE)["sha256"]   # not recorded there; P5 is the fixed baseline, never edited
            for child, k in (summary.get("children") or {}).items():
                if k.get("status") != "done":
                    continue
                entry["prompts"] = sorted(set(entry["prompts"]) | {k.get("prompt_id")} - {None})
                for v, x in (k.get("videos") or {}).items():   # only what the run's own caches did not give
                    for pid, sha, ans in ((k.get("prompt_id"), k.get("prompt_sha256"),
                                           {"verdict": x.get("final"), "model_verdict": x.get("model_verdict"),
                                            "score": x.get("score"), "rule_override": x.get("rule_override")}),
                                          (pv.BASELINE, p5_sha, {"verdict": x.get("p5_final"), "model_verdict": x.get("p5_model"),
                                                               "score": x.get("p5_score")})):
                        if pid and ans["verdict"] and (child, slug, pid, v) not in seen:
                            samples.append(_sample(pid, sha, slug, rec["source"], child, v, f"{child}|{slug}",
                                                   f"_collections/{cid}/test_summary.json", ans, reasons=False))
            if report is None:
                report = rct.build_report(summary)     # in memory; nothing written
        if report:
            entry["report"] = {k: report.get(k) for k in ("basis", "runs", "accuracy", "false_scale", "winners_caught",
                                                          "agreement_p5_p6", "gate_pass_rate", "score_only")}
        out_cols.append(entry)
    return {"sessions": sessions, "samples": samples, "humans": humans, "adoptions": list(adoptions.values()),
            "collections": out_cols}


# ---------------------------------------------------------------- measuring

def _truths(sessions, uploads):
    """Per source: evaluate.truth() of the source folder, else of any of its runs (each holds the locked copy)."""
    out = {}
    for src in sorted({s["source"] for s in sessions.values()}):
        folders = [uploads / src] + [uploads / s["id"] for s in sessions.values() if s["source"] == src]
        out[src] = next((t for t in (evaluate.truth(f) for f in folders if f.is_dir()) if t), {})
    return out


def _label(pid, sha, current):
    cur = current.get(pid)
    return pid if sha is not None and sha == cur else f"{pid}@{(sha or 'unknown')[:8]}"


def _ok(s, keys):
    """(exact-verdict right, scale-call right on the basis, kind) for one sample; None where there is no key."""
    k = keys.get(s["video"])
    if not k:
        return None, None, None
    exp = k["expected" if s["prompt_id"] == pv.BASELINE else "expected_multimodal"]
    truth = k["cac_call"] if k["basis"] == "cac_outcome" else _scale(exp)
    exact = None if exp is None else s["final"] == exp
    call = None if truth is None else _scale(s["final"]) == truth
    kind = ("false_scale" if s["final"] == "SCALE" and truth == "DO_NOT_SCALE"
            else "missed_winner" if truth == "SCALE" and s["final"] != "SCALE"
            else "other_mismatch" if exact is False else None)
    return exact, call, kind


def _reason_text(reasons):
    if isinstance(reasons, list):
        return "\n".join(f"{i}. {r}" for i, r in enumerate(reasons, 1))
    return str(reasons) if reasons else None


def measure(data, truths, current, samples_n):
    groups = defaultdict(list)
    for s in data["samples"]:
        groups[_label(s["prompt_id"], s["prompt_sha256"], current)].append(s)
    prompts = {}
    for label, ss in sorted(groups.items(), key=lambda kv: (kv[0] != pv.BASELINE, kv[0])):
        mm = ss[0]["prompt_id"] != pv.BASELINE
        runs = defaultdict(dict)
        for s in ss:
            runs[(s["source"], s["run"])][s["video"]] = s["final"]
        per_run, partial = [], 0
        for (src, run), verdicts in sorted(runs.items()):
            keys = truths.get(src) or {}
            if not keys:
                continue
            if set(keys) - set(verdicts):
                partial += 1                       # a run that did not score every video is not comparable
                continue
            m = evaluate.verdict_metrics(verdicts, keys, mm)
            b = m[_basis(keys)]
            got = b["n"] > 0                        # no video with a key: not measured, never 0 false SCALE
            per_run.append({"source": src, "run": run, "exact_accuracy": m["expected_verdict"]["accuracy"],
                            "accuracy": b["accuracy"], "fp": b["fp"] if got else None, "tp": b["tp"] if got else None,
                            "winners": b["winners"] if got else None, "recall": b["recall"], "n": b["n"]})
        videos = {}
        for (src, vid), vs in sorted(_by(ss, lambda s: (s["source"], s["video"])).items()):
            keys = {v: dict(k, basis=_basis(truths[src])) for v, k in (truths.get(src) or {}).items()}
            checks = [(s,) + _ok(s, keys) for s in vs]
            fin = Counter(s["final"] for s in vs)
            modal, top = fin.most_common(1)[0]
            k = keys.get(vid) or {}
            wrong = [c for c in checks if c[1] is False or c[2] is False]
            judged = [c for c in checks if c[1] is not None or c[2] is not None]
            v = {"source": src, "video": vid, "n": len(vs), "scenario": k.get("scenario"),
                 "expected": k.get("expected_multimodal" if mm else "expected"),
                 "basis_truth": (k.get("cac_call") if k.get("basis") == "cac_outcome" else _scale(k.get("expected_multimodal" if mm else "expected"))) if k else None,
                 "final": dict(fin), "model_verdict": dict(Counter(s["model_verdict"] for s in vs)),
                 "score": rct._stats([s["score"] for s in vs]), "modal": modal, "modal_share": round(top / len(vs), 3),
                 "rule_overrides": sum(1 for s in vs if s["rule_override"]),
                 "wrong": len(wrong) if judged else None, "wrong_share": round(len(wrong) / len(judged), 3) if judged else None,
                 "wrong_kinds": dict(Counter(c[3] for c in wrong if c[3]))}
            mod_wrong = any(c[0]["final"] == modal for c in wrong)
            if judged and (mod_wrong or v["wrong_share"] >= FREQUENT_WRONG):
                v["evidence"] = _pick(wrong, samples_n)
                v["humans"] = list(data["humans"].get((src, vid), {}).values())
            videos[f"{src}/{vid}"] = v
        prompts[label] = {
            "label": label, "prompt_id": ss[0]["prompt_id"], "prompt_sha256": ss[0]["prompt_sha256"],
            "current_text": label == ss[0]["prompt_id"], "multimodal": mm, "samples": len(ss),
            "with_reasons": sum(1 for s in ss if s["reasons"]), "runs": len(runs), "complete_runs": len(per_run),
            "partial_runs": partial, "videos": len({s["video"] for s in ss}),
            "sources": dict(Counter(s["source"] for s in ss)), "llms": dict(Counter(s["llm"] for s in ss)),
            "accuracy": rct._stats([r["accuracy"] for r in per_run]),
            "exact_accuracy": rct._stats([r["exact_accuracy"] for r in per_run]),
            "false_scale": dict(rct._stats([r["fp"] for r in per_run]), runs_with_any=sum(1 for r in per_run if r["fp"])),
            "winners_caught": dict(rct._stats([r["tp"] for r in per_run]),
                                   winners=sorted({r["winners"] for r in per_run} - {None}) or None),
            "stability": rct._stats([v["modal_share"] for v in videos.values()]),
            "per_run": per_run, "per_video": videos}
    return prompts


def _by(xs, key):
    out = defaultdict(list)
    for x in xs:
        out[key(x)].append(x)
    return out


def _basis(keys):
    return "cac_outcome" if any(k.get("cac_call") for k in keys.values()) else "expected_scale"


def _pick(wrong, n):
    """Up to n wrong answers with reasons, most common wrong verdict first, distinct reason texts."""
    freq = Counter(c[0]["final"] for c in wrong)
    out, texts = [], set()
    for s, _, _, kind in sorted(wrong, key=lambda c: (-freq[c[0]["final"]], c[0]["origin"])):
        t = _reason_text(s["reasons"])
        if not t or t in texts:
            continue
        texts.add(t)
        out.append({"final": s["final"], "model_verdict": s["model_verdict"], "score": s["score"], "kind": kind,
                    "origin": s["origin"], "llm": s["llm"], "reasons": t, "guard_flag": guard.scan(t)})
        if len(out) >= n:
            break
    return out


def _mean_dict(p):
    """A prompt's per-run means shaped like evaluate.confusion(), for evaluate.beats; None when not measured."""
    fp, acc, tp = p["false_scale"]["mean"], p["accuracy"]["mean"], p["winners_caught"]["mean"]
    if fp is None or acc is None:
        return None
    w = (p["winners_caught"]["winners"] or [None])[0]
    return {"n": p["false_scale"]["n"], "fp": fp, "accuracy": acc,
            "recall": round(tp / w, 3) if tp is not None and w else None}


def _beats_p5(p, base):
    """evaluate.beats on per-run means; None (not available) when either side was not measured."""
    c, b = _mean_dict(p), _mean_dict(base) if base else None
    return evaluate.beats(c, b) if c and b else None


def ranking(prompts, sources):
    """P6-family prompts with complete runs, by the gate's order: fewest false SCALE per run, then accuracy, then
    winners caught; beats_p5 is evaluate.beats on per-run means of the same source(s)."""
    out = {}
    for scope in [None] + sorted(sources):
        def pick(p):
            runs = [r for r in p["per_run"] if scope is None or r["source"] == scope]
            return dict(p, per_run=runs, complete_runs=len(runs), accuracy=rct._stats([r["accuracy"] for r in runs]),
                        false_scale=rct._stats([r["fp"] for r in runs]),
                        winners_caught=dict(rct._stats([r["tp"] for r in runs]),
                                            winners=sorted({r["winners"] for r in runs} - {None}) or None))
        ps = {k: pick(p) for k, p in prompts.items()}
        base = ps.get(pv.BASELINE)
        rows = []
        for k, p in ps.items():
            if not p["multimodal"] or not p["complete_runs"]:
                continue
            rows.append({"prompt": k, "current_text": p["current_text"], "runs": p["complete_runs"],
                         "sources": sorted({r["source"] for r in p["per_run"]}),
                         "false_scale_mean": p["false_scale"]["mean"], "accuracy_mean": p["accuracy"]["mean"],
                         "winners_caught_mean": p["winners_caught"]["mean"], "beats_p5": _beats_p5(p, base)})
        inf = float("inf")                         # not measured sorts last, never as 0
        rows.sort(key=lambda r: (not r["current_text"], inf if r["false_scale_mean"] is None else r["false_scale_mean"],
                                 -(r["accuracy_mean"] if r["accuracy_mean"] is not None else -inf),
                                 -(r["winners_caught_mean"] if r["winners_caught_mean"] is not None else -inf)))
        notes = []
        if scope is None and len({tuple(c["sources"]) for c in rows} | ({tuple(sorted({r["source"] for r in base["per_run"]}))}
                                                                          if base and base["complete_runs"] else set())) > 1:
            notes.append("prompts here were measured on different sources/campaigns: the pooled means are not paired")
        if scope is not None and base:
            notes += [f"{c['prompt']} has {c['runs']} runs vs P5's {base['complete_runs']} on this source: unpaired means"
                      for c in rows if c["runs"] > base["complete_runs"]]
        out[scope or "all"] = {"notes": notes, "p5": ({"runs": base["complete_runs"], "false_scale_mean": base["false_scale"]["mean"],
                                       "accuracy_mean": base["accuracy"]["mean"],
                                       "winners_caught_mean": base["winners_caught"]["mean"]}
                                      if base and base["complete_runs"] else None), "candidates": rows}
    return out


def risks(prompts, truths, rank=None):
    out = [n for r in (rank or {}).values() for n in r.get("notes") or []]
    vids = {v for t in truths.values() for v in t}
    if len(vids) < SMALL["videos"]:
        out.append(f"only {len(vids)} distinct videos with ground truth: a prompt change tuned on them can overfit; "
                   f"prefer general rules over video-specific ones")
    if len([t for t in truths.values() if t]) < SMALL["sources"]:
        out.append(f"ground truth from {len([t for t in truths.values() if t])} source campaign(s) only")
    for k, p in prompts.items():
        if p["multimodal"] and p["complete_runs"] < SMALL["runs"]:
            out.append(f"{k}: {p['complete_runs']} complete run(s): too few samples to separate a real gain from noise")
        if len(p["llms"]) > 1:
            out.append(f"{k}: answers from {len(p['llms'])} models are pooled ({', '.join(p['llms'])})")
        if not p["current_text"]:
            out.append(f"{k}: answers from a prompt text that is not today's {p['prompt_id']} (or unknown): context only")
    shared = Counter(v for t in truths.values() for v in t)
    if any(n > 1 for n in shared.values()):
        out.append("some sources share the same video ids (copies of one campaign): they are not independent evidence")
    return out


# ---------------------------------------------------------------- output

def build(source=None, prompt_ids=None, samples_n=3):
    uploads = config.UPLOADS
    data = scan(uploads)
    if source:
        data["samples"] = [s for s in data["samples"] if source in (s["source"], s["session"])]
    if prompt_ids:
        data["samples"] = [s for s in data["samples"] if s["prompt_id"] in prompt_ids]
    vs, migrated = versions()
    by_id = {v["id"]: v for v in vs}
    current = {v["id"]: v["sha256"] for v in vs}
    truths = _truths(data["sessions"], uploads)
    used = {s["source"] for s in data["samples"]}
    truths = {k: v for k, v in truths.items() if k in used}
    prompts = measure(data, truths, current, samples_n)
    rank = ranking(prompts, used)
    ok = [r for r in rank["all"]["candidates"] if r["current_text"]]
    parent = next((r["prompt"] for r in ok if r["runs"] >= SMALL["runs"]), ok[0]["prompt"] if ok else None)
    names = {s["id"]: s["name"] for s in data["sessions"].values()}
    return {
        "generated_at": _now(), "filters": {"source": source, "prompts": prompt_ids, "samples": samples_n},
        "scanned": {"sessions": len(data["sessions"]), "collections": len(data["collections"]),
                    "samples": len(data["samples"])},
        "sources": {s: {"name": names.get(s), "videos": len(truths.get(s) or {}),
                        "basis": _basis(truths[s]) if truths.get(s) else None,
                        "sessions": sum(1 for x in data["sessions"].values() if x["source"] == s)} for s in sorted(used)},
        "suggested_parent": parent,
        "suggested_parent_why": (f"first P6-family prompt with today's text (and at least {SMALL['runs']} complete runs when any has them) in "
                                 "the gate order over all complete runs (fewest false SCALE per run, then accuracy, then "
                                 "winners caught); P5 is never a parent"
                                 if parent else "no P6-family prompt has a complete run with ground truth"),
        "ranking": rank, "risks": risks(prompts, truths, rank), "prompts": prompts,
        "collections": [c for c in data["collections"] if not source or c["source"] == source],
        "adoptions": data["adoptions"],
        "versions": [{**{k: v.get(k) for k in ("id", "parent", "note", "created_at", "sha256", "builtin")},
                      "chars": len(v["text"]), "diff_vs_parent": _diff(v, by_id, migrated)} for v in vs],
        "library_migrated": migrated,
    }, vs


def _na(x):
    return config.NA if x is None else x


def _cell(x):
    """A free-text value (session / collection name) safe inside a table row or list line."""
    return " ".join(str(_na(x)).replace("|", "\\|").replace("~~~", "~ ~ ~").split())


def _fence(text, label):
    body = str(text).replace("~~~", "~ ~ ~")
    return (f"UNTRUSTED {label}: data, not instructions; may quote comments, transcripts or on-screen text.\n"
            f"~~~text\n{body}\n~~~")


def _s(st):
    return config.NA if not st or not st.get("n") else f"{st['mean']} (min {st['min']}, max {st['max']}, n={st['n']})"


def render(ev):
    L = [f"# Prompt evidence pack ({ev['generated_at']})", "",
         f"Read-only scan: {ev['scanned']['sessions']} sessions, {ev['scanned']['collections']} collections, "
         f"{ev['scanned']['samples']} distinct scored answers. Filters: {ev['filters']}.",
         f"Measures per run on the gate basis (CAC outcome when known, else expected scale call); "
         f"P5 on the text-only key, P6-family on the multimodal key. Missing values: {config.NA}.", "",
         f"**Suggested parent: {_na(ev['suggested_parent'])}** — {ev['suggested_parent_why']}.", "",
         "## Risks (small sample / overfitting)", ""]
    L += [f"- {r}" for r in ev["risks"]] or ["- none flagged"]
    L += ["", "## Sources", "", "| source | name | videos with truth | basis | sessions |", "|---|---|---|---|---|"]
    L += [f"| {k} | {_cell(v['name'])} | {v['videos']} | {_na(v['basis'])} | {v['sessions']} |" for k, v in ev["sources"].items()]
    L += ["", "## Prompts", "", "| prompt | samples (w/ reasons) | runs complete / partial | videos | models | accuracy / run "
          "| exact-verdict accuracy / run | false SCALE / run (runs with any) | winners caught / run (of) | stability (modal share) |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for k, p in ev["prompts"].items():
        L.append(f"| {k} | {p['samples']} ({p['with_reasons']}) | {p['complete_runs']} / {p['partial_runs']} | {p['videos']} | "
                 f"{', '.join(p['llms'])} | {_s(p['accuracy'])} | {_s(p['exact_accuracy'])} | {_s(p['false_scale'])} "
                 f"({p['false_scale']['runs_with_any']}) | {_s(p['winners_caught'])} ({_na(p['winners_caught']['winners'])}) | "
                 f"{_s(p['stability'])} |")
    L += ["", "## Gate ranking (fewest false SCALE, then accuracy, then winners caught)", ""]
    for scope, r in ev["ranking"].items():
        p5 = r["p5"]
        line = lambda c: (f"false SCALE {_na(c['false_scale_mean'])}, accuracy {_na(c['accuracy_mean'])}, "
                          f"winners {_na(c['winners_caught_mean'])}, runs {c['runs']}")
        L.append(f"**{scope}** — P5: " + (line(p5) if p5 else config.NA))
        cur = [c for c in r["candidates"] if c["current_text"]]
        old = [c for c in r["candidates"] if not c["current_text"]]
        L += [f"{i}. {c['prompt']}: {line(c)}, beats P5: {_na(c['beats_p5'])}" for i, c in enumerate(cur, 1)] \
            or ["(no current-text candidate with complete runs)"]
        if old:
            L += ["", "Stale / legacy text (not today's prompt text; context only, never a parent):"]
            L += [f"- {c['prompt']}: {line(c)}, beats P5: {_na(c['beats_p5'])}" for c in old]
        L += [f"- note: {n}" for n in r.get("notes") or []]
        L.append("")
    L += ["## Collections", ""]
    for c in ev["collections"]:
        r = c["report"] or {}
        L.append(f"- {c['id']} \"{_cell(c['name'])}\" ({c['style']}, source {c['source']}, prompts {', '.join(c['prompts']) or config.NA}, "
                 f"{c['runs']} runs): accuracy P6 {_s((r.get('accuracy') or {}).get('p6'))} vs P5 {_s((r.get('accuracy') or {}).get('p5'))}; "
                 f"false SCALE P6 {_s((r.get('false_scale') or {}).get('p6'))} vs P5 {_s((r.get('false_scale') or {}).get('p5'))}; "
                 f"gate pass rate {_na(r.get('gate_pass_rate'))}")
    L += ["", "## Per-video verdicts", ""]
    for k, p in ev["prompts"].items():
        L += [f"### {k}", "", "| video | scenario | expected | gate truth | final verdicts | model verdicts | score | modal share | wrong |",
              "|---|---|---|---|---|---|---|---|---|"]
        for key, v in p["per_video"].items():
            fmt = lambda c: ", ".join(f"{a} {n}" for a, n in sorted(c.items(), key=lambda kv: -kv[1]))
            L.append(f"| {key} | {_na(v['scenario'])} | {_na(v['expected'])} | {_na(v['basis_truth'])} | {fmt(v['final'])} | "
                     f"{fmt(v['model_verdict'])} | {_na(v['score']['mean'])} | {v['modal_share']} | "
                     f"{_na(v['wrong'])}/{v['n']} {v['wrong_kinds'] or ''} |")
        L.append("")
    L += ["## Wrong verdicts: model reasons and human decisions", ""]
    for k, p in ev["prompts"].items():
        for key, v in p["per_video"].items():
            if "evidence" not in v:
                continue
            L += [f"### {k} · {key}", f"expected {_na(v['expected'])}, gate truth {_na(v['basis_truth'])}, modal {v['modal']} "
                  f"({v['modal_share']}), wrong {v['wrong']}/{v['n']} {v['wrong_kinds']}", ""]
            for e in v["evidence"]:
                L += [f"- {e['final']} (model {e['model_verdict']}, score {_na(e['score'])}, {e['kind']}, {e['llm']}, "
                      f"{e['origin']}){' — injection guard pattern matched' if e['guard_flag'] else ''}",
                      _fence(e["reasons"], "MODEL REASONS"), ""]
            if not v["evidence"]:
                L += ["(no cached reasons for these answers)", ""]
            if v.get("humans"):
                L += [_fence("\n".join(f"{h['kind']} · {_na(h['reviewer'])}: {_na(h['decision'])}"
                                       + (f" (AI said {h['ai_verdict']})" if h.get("ai_verdict") else "")
                                       + (f" — {h['note']}" if h.get("note") else "") for h in v["humans"]),
                             "HUMAN DECISIONS"), ""]
    L += ["## Adoption log", ""]
    for a in ev["adoptions"]:
        L += [f"- {_cell(a.get('at'))} {_cell(a.get('id'))} in {_cell(a.get('session'))}: beats_baseline "
              f"{_na(a.get('beats_baseline'))}",
              _fence(f"why: {_na(a.get('why'))}\noverride reason: {_na(a.get('override_reason'))}", "ADOPTION LOG TEXT"), ""]
    if not ev["adoptions"]:
        L.append("(none)")
    L += ["", "## Prompt versions (full texts in prompts/<id>.txt)", ""]
    for v in ev["versions"]:
        L += [f"### {v['id']} (parent {_na(v['parent'])}, sha {v['sha256'][:12]}, {v['chars']} chars"
              f"{', built in' if v['builtin'] else ''})", _fence(v["note"] or "", "LIBRARY NOTE"), ""]
        if v["diff_vs_parent"]:
            L += ["~~~diff", v["diff_vs_parent"].replace("~~~", "~ ~ ~").rstrip("\n"), "~~~", ""]
    return "\n".join(L) + "\n"


_SAFE_ID = re.compile(r"^P\d+(\.\d+)?$")


def _under(out, up):
    """Whether `out` is uploads/ or inside it: by file identity on every existing ancestor (catches case variants
    on a case-insensitive filesystem and symlinks), and by case-folded path as a fallback."""
    if out == up or up in out.parents or str(out).casefold() == str(up).casefold() \
            or str(out).casefold().startswith(str(up).casefold() + os.sep):
        return True
    if not up.exists():
        return False
    for a in (out, *out.parents):
        try:
            if a.exists() and os.path.samefile(a, up):
                return True
        except OSError:
            continue
    return False


def write(ev, vs, out):
    out = Path(out).resolve()
    up = config.UPLOADS.resolve()
    if _under(out, up):
        raise SystemExit(f"--out {out} is under uploads/; write the evidence somewhere else")
    targets = [out / "prompts", out / "evidence.json", out / "evidence.md"] + [out / "prompts" / f"{v['id']}.txt" for v in vs]
    if any(p.is_symlink() for p in targets):
        raise SystemExit(f"--out {out}: an output file or prompts/ is a symlink; use an empty folder")
    bad = [v["id"] for v in vs if not _SAFE_ID.match(str(v["id"]))]
    if bad:
        raise SystemExit(f"prompt id(s) not usable as file names: {', '.join(map(repr, bad))}")
    (out / "prompts").mkdir(parents=True, exist_ok=True)
    for v in vs:
        (out / "prompts" / f"{v['id']}.txt").write_text(v["text"], encoding="utf-8")
    (out / "evidence.json").write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "evidence.md").write_text(render(ev), encoding="utf-8")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=None, help="output folder (default: a new folder under the system temp dir)")
    ap.add_argument("--source", default=None, help="only this source session (or one of its runs)")
    ap.add_argument("--prompts", default=None, help="comma-separated prompt ids, e.g. P5,P6,P6.2")
    ap.add_argument("--samples", type=int, default=3, help="model reasons quoted per wrong video (default 3)")
    args = ap.parse_args(argv)
    ids = [x.strip() for x in args.prompts.split(",") if x.strip()] if args.prompts else None
    ev, vs = build(args.source, ids, max(0, args.samples))
    out = write(ev, vs, args.out or tempfile.mkdtemp(prefix="partgram-prompt-evidence-"))
    print(f"suggested parent: {ev['suggested_parent']} · {ev['scanned']['samples']} answers · "
          f"{len(ev['prompts'])} prompt groups\nsaved {out / 'evidence.json'}\nsaved {out / 'evidence.md'}", flush=True)
    return ev


if __name__ == "__main__":
    main()
