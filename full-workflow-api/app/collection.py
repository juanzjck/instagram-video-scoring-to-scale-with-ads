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
from datetime import datetime, timezone

from . import config, dataset

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
    tmp = d / (FILE + ".tmp")
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
    if n < 1 or len(rec["children"]) + n > MAX_CHILDREN:
        raise ValueError(f"a collection holds 1 to {MAX_CHILDREN} runs")
    new = []
    for _ in range(n):
        copy = dataset.copy_session(rec["source"])            # reads the source, never writes it
        index = len(rec["children"]) + 1
        p = config.UPLOADS / copy["id"] / dataset.SESSION_FILE
        s = json.loads(p.read_text(encoding="utf-8"))
        s.update(name=child_name(rec["name"], index), name_suffix="",
                 collection={"id": cid, "name": rec["name"], "index": index})
        dataset._save_session(s)
        rec["children"].append(copy["id"])
        new.append(copy["id"])
        _write(rec)                                            # after each copy: an interruption loses nothing
    return new


def _children_present(rec):
    return [c for c in rec["children"] if (config.UPLOADS / c).is_dir()]


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
        "children": len(present), "missing_children": len(rec["children"]) - len(present),
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
