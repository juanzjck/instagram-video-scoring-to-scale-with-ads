"""Collections: a parent that groups many copies ("runs") of one source session.

Used to repeat the whole workflow on the same campaign many times (e.g. 100 runs of "XTB · 1") and
measure how stable the verdicts are. Each run is an ordinary session made with dataset.copy_session,
so the source is only ever read, and every run keeps its own runs/, iterations and decisions. A run
knows its parent through session.json "collection": {"id", "name", "index"}.

The parent itself is not a session: it lives in uploads/_collections/<id>/collection.json (the
leading underscore keeps it out of the session list), next to whatever a batch writes about the
collection (scripts/run_collection_test.py keeps its summary and report there).
"""

import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone

from . import config, dataset, evaluate

FILE = "collection.json"
_ID_RE = re.compile(r"^col-[0-9]{8}-[0-9]{6}-[0-9]{6}$")
MAX_CHILDREN = 1000


def root():
    return config.UPLOADS / "_collections"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def folder(cid):
    if not _ID_RE.match(cid or ""):
        raise KeyError(cid)
    d = root() / cid
    if not (d / FILE).is_file():
        raise KeyError(cid)
    return d


def _read(cid):
    return json.loads((folder(cid) / FILE).read_text(encoding="utf-8"))


def _write(rec):
    d = root() / rec["id"]
    d.mkdir(parents=True, exist_ok=True)
    rec["updated_at"] = _now()
    tmp = d / f"{FILE}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(d / FILE)
    return rec


def child_name(name, index):
    return f"{name} · run {index:03d}"[:120]


def create(source, name, children=0):
    """A new, empty collection of `source` (a saved session id), then `children` runs."""
    name = (name or "").strip()[:100]
    if not name:
        raise ValueError("give the collection a name")
    if not config.DATASET_ID_RE.match(source or "") or not (config.UPLOADS / source).is_dir():
        raise ValueError(f"unknown session {source!r}")
    if (dataset._load_session(config.UPLOADS / source)).get("collection"):
        raise ValueError("the source is itself a run of a collection; use the original session")
    cid = "col-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    _write({"id": cid, "name": name, "source": source, "created_at": _now(), "children": []})
    if children:
        add_children(cid, children)
    return get(cid)


def find(name, source=None):
    """The collection with this name (and source, when given), or None."""
    for rec in _all():
        if rec["name"] == name and (source is None or rec["source"] == source):
            return rec
    return None


def add_children(cid, n):
    """Make n more runs of the collection's source. Returns the new child ids."""
    n = int(n)
    rec = _read(cid)
    if n < 1 or len(run_ids(rec)) + n > MAX_CHILDREN:
        raise ValueError(f"a collection holds 1 to {MAX_CHILDREN} runs")
    new = []
    for _ in range(n):
        copy = dataset.copy_session(rec["source"])            # reads the source, never writes it
        index = next_index(cid)
        p = config.UPLOADS / copy["id"] / dataset.SESSION_FILE
        s = json.loads(p.read_text(encoding="utf-8"))
        s.update(name=child_name(rec["name"], index), name_suffix="",
                 collection={"id": cid, "name": rec["name"], "index": index})
        dataset._save_session(s)
        rec["children"].append(copy["id"])
        rec["next_index"] = index + 1
        new.append(copy["id"])
        _write(rec)                                            # after each copy: an interruption loses nothing
    return new


def run_ids(rec):
    """Every run of the collection: its source when that is run 001 (auto-created parents), then the children."""
    return ([rec["source"]] if rec.get("source_is_run") else []) + list(rec["children"])


def _children_present(rec):
    return [c for c in run_ids(rec) if (config.UPLOADS / c).is_dir()]


def _all():
    d = root()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob(f"*/{FILE}")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return out


def _test_summary(cid):
    p = root() / cid / "test_summary.json"
    if not p.is_file():
        return None
    try:
        t = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    states = [c.get("status") for c in (t.get("children") or {}).values()]
    return {"updated_at": t.get("updated_at"), "llm": t.get("llm"), "vision": t.get("vision"),
            "done": states.count("done"), "error": states.count("error"),
            "running": states.count("running"), "report": (root() / cid / "report.json").is_file()}


def _view(rec, details=False):
    present = _children_present(rec)
    out = {k: rec.get(k) for k in ("id", "name", "source", "created_at", "updated_at")} | {
        "children": len(present), "missing_children": len(run_ids(rec)) - len(present),
        "source_name": dataset._load_session(config.UPLOADS / rec["source"]).get("name")
        if (config.UPLOADS / rec["source"]).is_dir() else None,
        "test": _test_summary(rec["id"])}
    if details:
        out["child_ids"] = present
        rp = root() / rec["id"] / "report.json"
        out["report"] = json.loads(rp.read_text(encoding="utf-8")) if rp.is_file() else None
    return out


def listing():
    return [_view(r) for r in sorted(_all(), key=lambda r: r.get("updated_at") or r["id"], reverse=True)]


def get(cid):
    return _view(_read(cid), details=True)


# ---------------------------------------------------------------- evaluation of all runs

_eval_lock = threading.Lock()          # evaluate.run reads the open session from config, so one switch at a time


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def _aggregate(runs):
    """Per prompt row, across the runs that have a value: mean / min / max accuracy and mean recall, vs the
    expected verdict and vs the CAC outcome. A run without a value (blank, not scored) is left out, never 0."""
    out = {}
    for name in dict.fromkeys(k for r in runs for k in (r.get("verdict_accuracy") or {})):
        rows = [r["verdict_accuracy"][name] for r in runs if name in (r.get("verdict_accuracy") or {})]
        entry = {}
        for measure, pick in (("expected_verdict", lambda a: a), ("cac_outcome", lambda a: a.get("cac") or {})):
            vals = [pick(a) for a in rows if pick(a).get("n")]
            accs = [a["accuracy"] for a in vals]
            entry[measure] = {"runs": len(vals), "mean_accuracy": _mean(accs), "min_accuracy": min(accs, default=None),
                              "max_accuracy": max(accs, default=None), "mean_recall": _mean(a.get("recall") for a in vals)}
        out[name] = entry
    return out


def evaluations(cid):
    """Evaluate every run of the collection from what is already cached (no model call, nothing scored).

    Each run is opened in turn as the active session, so evaluate.run reads that run's own scores, ground
    truth copy and thresholds; the previously open session is restored afterwards. A run that fails is
    reported with its error instead of stopping the others."""
    rec = _read(cid)
    runs = []
    with _eval_lock:
        before = config.DATASET_ID
        try:
            for child in _children_present(rec):
                row = {"id": child, "name": None, "index": None, "llm": {}}
                try:
                    s = dataset._load_session(config.UPLOADS / child)
                    row.update(name=s.get("name"), index=(s.get("collection") or {}).get("index"),
                               llm=config.resolve_llm(s.get("llm")))
                    dataset._activate(child)
                    from . import runs as runs_mod             # local: runs imports this module
                    info = runs_mod.current()
                    e = evaluate.run(run_missing=False, prompt_id=runs_mod.view_prompt())   # each run on its own prompt
                    pid = e["prompt"]
                    row.update(scored=sum(1 for v in e["videos"] if v["p6_final"]), videos=len(e["videos"]), error=None,
                               run=dict(info or {}, prompt_id=pid) if info or pid else None,
                               verdict_accuracy={(pid + k[2:] if k.startswith("P6") else k): v    # "P6.2 multimodal, ..."
                                                 for k, v in e["verdict_accuracy"].items()})
                except Exception as ex:                         # one bad run must not hide the rest
                    row.update(scored=None, videos=None, verdict_accuracy=None, run=None, error=f"{type(ex).__name__}: {ex}")
                runs.append(row)
        finally:
            try:
                dataset._activate(before)
            except ValueError:                                  # the session was deleted meanwhile
                dataset._activate(None)
    runs.sort(key=lambda r: r["index"] or 0)
    ok = [r for r in runs if not r["error"]]
    return {"id": rec["id"], "name": rec["name"], "runs": runs, "evaluated": len(ok),
            "failed": len(runs) - len(ok), "aggregate": _aggregate(ok)}


# ---------------------------------------------------------------- reviews of all runs

def _unique_calls(allc):
    """One entry per reviewer: their distinct calls (reviewer + decision + reason, case/space-insensitive) as
    `variants`, each with the runs it came from (numeric run order) and a count. Keyed by the real reviewer name."""
    out = {}
    for _, tag, who, c in allc:
        e = out.setdefault(who, {"reviewer": who, "count": 0, "variants": []})
        key = (c["decision"], " ".join(str(c["reason"] or "").lower().split()))
        v = next((x for x in e["variants"] if x["key"] == key), None)
        if v is None:
            v = {"key": key, "decision": c["decision"], "reason": c["reason"], "runs": [], "count": 0}
            e["variants"].append(v)
        v["runs"].append(tag.removeprefix("run "))
        v["count"] += 1
        e["count"] += 1
    for e in out.values():
        for v in e["variants"]:
            del v["key"]
    return dict(sorted(out.items()))


def reviews(cid, unique=False):
    """Every run's submitted manual reviews, per video, in one view (drafts stay hidden, as in dataset.reviews).

    Each run is opened in turn like evaluations() does. A call is labelled "run 001 · Ana"; when every call
    on a video is identical (decision + reason) they collapse into one `merged` entry. A run that fails is
    reported with its error. Nothing is written.

    unique=True collapses repeats, which copy_session makes (every run starts with the source's sheets): per video,
    calls with the same reviewer + decision + reason (case/space-insensitive, minutes ignored) become one entry
    keyed by the reviewer, whose distinct calls are listed as `variants` (decision, reason, runs, count). Agreement
    counts distinct reviewers: under 2 is None; a reviewer with 2+ distinct calls is a split; otherwise agree when all
    decisions are equal, and `merged` when decision and reason are equal too."""
    rec = _read(cid)
    runs, videos, cols = [], {}, {}
    with _eval_lock:
        before = config.DATASET_ID
        try:
            for child in _children_present(rec):
                row = {"id": child, "name": None, "index": None, "reviewers": [], "error": None}
                runs.append(row)
                try:
                    s = dataset._load_session(config.UPLOADS / child)
                    row.update(name=s.get("name"), index=(s.get("collection") or {}).get("index"))
                    dataset._activate(child)
                    r = dataset.reviews()
                except Exception as ex:
                    row["error"] = f"{type(ex).__name__}: {ex}"
                    continue
                row["reviewers"] = r["reviewers"]
                tag = f"run {row['index']:03d}" if row["index"] else f"run {child}"
                for v in r["videos"]:
                    out = videos.setdefault(v["video_id"], {k: v[k] for k in ("video_id", "creator", "caption", "url", "clip", "info")}
                                            | {"calls": {}, "expected": None})
                    out["expected"] = out["expected"] or v["expected"]
                    for who, c in v["calls"].items():
                        out.setdefault("_all", []).append(((row["index"] is None, row["index"] or 0, child), tag, who,
                                                           c | {"run": child, "reviewer": who}))
        finally:
            try:
                dataset._activate(before)
            except ValueError:
                dataset._activate(None)
    runs.sort(key=lambda r: r["index"] or 0)
    for v in videos.values():
        allc = sorted(v.pop("_all", []), key=lambda t: (t[0], t[2]))      # runs in numeric order, id fallbacks last
        if unique:
            v["calls"] = _unique_calls(allc)
            n = len(v["calls"])
            flat = {w: e["variants"][0] for w, e in v["calls"].items() if len(e["variants"]) == 1}
            picks = {x["decision"] for e in v["calls"].values() for x in e["variants"]}
            v["merged"] = dataset.merge_calls(flat) if len(flat) == n else None
            v["agreement"] = (None if n < 2 else "split" if len(flat) < n or len(picks) > 1 else "agree")
            continue
        v["calls"] = {f"{tag} · {who}": c for _, tag, who, c in allc}            # already in run order
        cols.update({f"{tag} · {who}": (k, who) for k, tag, who, _ in allc})
        picks = {c["decision"] for c in v["calls"].values()}
        v["merged"] = dataset.merge_calls(v["calls"])
        v["agreement"] = None if len(v["calls"]) < 2 else ("agree" if len(picks) == 1 else "split")
    out = list(videos.values())
    both = [v for v in out if v["agreement"]]
    return {"id": rec["id"], "name": rec["name"], "unique": bool(unique), "runs": runs, "videos": out,
            "columns": sorted({w for v in out for w in v["calls"]}) if unique else sorted(cols, key=cols.get),
            "agreement": {"agree": sum(v["agreement"] == "agree" for v in both), "n": len(both)}}


# ---------------------------------------------------------------- runs started from the app (app/runs.py)

def _index_of(sid):
    try:
        return int(((dataset._load_session(config.UPLOADS / sid).get("collection")) or {}).get("index") or 0)
    except (OSError, ValueError):
        return 0


def next_index(cid):
    """The number the next run of the collection gets: one past the highest run number present or ever handed out
    (`next_index` on the record), so a deleted or failed run's number is never reused."""
    rec = _read(cid)
    return max(max((_index_of(c) for c in run_ids(rec)), default=0) + 1, int(rec.get("next_index") or 1))


def run_records(cid):
    """[(index, session id, recorded run or None)] of every run present, in run order."""
    out = []
    for c in _children_present(_read(cid)):
        s = dataset._load_session(config.UPLOADS / c)
        out.append((_index_of(c), c, s.get("run")))
    return sorted(out, key=lambda t: t[0])


def ensure_for(sid):
    """The collection `sid` belongs to, creating a parent for it when it is a plain session: the session stays
    as it is and becomes run 001 of the new parent. Returns (collection id, created now)."""
    s = dataset._load_session(config.UPLOADS / sid)
    mark = s.get("collection")
    if mark:
        folder(mark["id"])                                     # KeyError when the parent is gone
        return mark["id"], False
    cid = "col-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    name = (s.get("name") or "Campaign")[:100]
    _write({"id": cid, "name": name, "source": sid, "source_is_run": True, "created_at": _now(), "children": []})
    s["collection"] = {"id": cid, "name": name, "index": 1}
    dataset._save_session(s)
    return cid, True


def add_run(cid, from_sid, keep_features=None):
    """A new run: a clean copy of `from_sid` (no scores, reviews or decisions; vision only when keep_features, a
    provider + model choice, is given). Returns (child id, its run number)."""
    rec = _read(cid)
    index = next_index(cid)
    copy = dataset.copy_session(from_sid, for_run=True, keep_features=keep_features)
    p = config.UPLOADS / copy["id"] / dataset.SESSION_FILE
    s = json.loads(p.read_text(encoding="utf-8"))
    s.update(name=child_name(rec["name"], index), name_suffix="", collection={"id": cid, "name": rec["name"], "index": index})
    dataset._save_session(s)
    rec["children"].append(copy["id"])
    rec["next_index"] = index + 1
    _write(rec)
    return copy["id"], index


def remove_run(cid, child):
    """Undo add_run (a start that could not go ahead leaves no empty child)."""
    rec = _read(cid)
    rec["children"] = [c for c in rec["children"] if c != child]
    _write(rec)
    if (config.UPLOADS / child).is_dir():
        dataset.delete_session(child)


def discard_if_empty(cid):
    """Drop an auto-created parent that never got a child, and the marker on its source."""
    rec = _read(cid)
    if rec["children"] or not rec.get("source_is_run"):
        return
    src = config.UPLOADS / rec["source"]
    if src.is_dir():
        s = dataset._load_session(src)
        s.pop("collection", None)
        dataset._save_session(s)
    shutil.rmtree(root() / cid, ignore_errors=True)
