"""Prompt versions: the tuning loop of the Section 6 workflow design.

After a campaign, the data/AI team reads the error report and hand-tunes P6. Each tuned prompt is a
new, immutable version (P6.2, P6.3, ...) kept in one global library, uploads/_prompts/library.json, so a
version is created once and any session can run it. What stays per session (session.json "prompts"):
which version the review card uses ("active") and the adoption log. A version is only adopted when an
iteration on that campaign shows it beats the P5 baseline (app/iterations.py); an override needs a
written reason and is logged. Score caches and iteration records are per session too.

Built in, never edited here: P5 (the fixed text-only baseline) and P6 (derived from P5 in prompts.py).
Every custom version is a P6-family prompt: it gets the multimodal summary (CONTENT FEATURES included).
"""

import difflib
import hashlib
import json
import logging
import re
import string
import threading
import uuid
from datetime import datetime, timezone

from . import config, prompts

BASELINE = "P5"
DEFAULT_ACTIVE = "P6"
MAX_CHARS = 100_000
MIN_REASON = 10                  # characters an adoption override reason needs
_ID_RE = re.compile(r"^P6\.\d+$")
log = logging.getLogger(__name__)


class Conflict(Exception):
    """The request is valid but the state refuses it (HTTP 409)."""


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _session_path():
    if config.DATA is None:
        raise ValueError("open a campaign first")
    return config.DATA / "session.json"


def _load():
    p = _session_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _write_json(p, rec):
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _store(session):
    session["updated_at"] = _now()
    _write_json(_session_path(), session)


def _state(session):
    st = session.setdefault("prompts", {})
    st.setdefault("adoptions", [])
    return st


# ---------------------------------------------------------------- the global library

_lock = threading.RLock()                 # id allocation and every read-modify-write of the library


def _lib_path():
    return config.UPLOADS / "_prompts" / "library.json"


def _sessions():
    """The saved sessions' folders (the library and collections, which start with "_", are not sessions)."""
    if not config.UPLOADS.is_dir():
        return []
    return sorted(f for f in config.UPLOADS.iterdir()
                  if f.is_dir() and config.DATASET_ID_RE.match(f.name) and (f / "session.json").is_file())


def _lib_read():
    p = _lib_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            raise ValueError(f"{p} is not valid JSON; fix or remove it") from None
    return {"next": 2, "versions": [], "migrated": False}


def _lib_write(lib):
    p = _lib_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_json(p, lib)


def _lib():
    """The library, after the one-time import of versions that older sessions kept in their session.json."""
    with _lock:
        lib = _lib_read()
        if not lib.get("migrated"):
            _migrate(lib)
            lib["migrated"] = True
            _lib_write(lib)
        return lib


def _bump(lib, pid):
    n = int(pid.split(".")[1])
    lib["next"] = max(lib.get("next", 2), n + 1)


def _migrate(lib):
    """Import per-session versions into the library, keyed by SHA-256, keeping their ids.

    Same text -> the existing id. A free id -> kept. An id taken by a different text -> the next free global
    id. Wherever a session's local id ends up meaning a different id, that session's active id and adoption
    log follow, and its cached answers under the old local id are dropped (cheap to re-score; they must never
    sit under an id that now means another text). Ground truth is never read or written.

    Crash-safe order: plan everything in memory, write the library (every version, no marker), then rewrite
    each session without its version list (atomic), and only then does the caller write the marker. A crash
    anywhere leaves the versions in the library, and a re-run finds an unstripped session's versions by SHA
    and reuses them. A session file that is not a JSON object, or a version entry without id or text, is skipped
    with a warning; recomputing the hash from the text means a missing or wrong stored hash cannot matter."""
    plans = []                                                   # (session.json path, session dict, ids to drop caches for)
    for folder in _sessions():
        sp = folder / "session.json"
        try:
            session = json.loads(sp.read_text(encoding="utf-8"))
        except ValueError:
            continue
        st = session.get("prompts") if isinstance(session, dict) else None
        if not isinstance(st, dict) or ("versions" not in st and "next" not in st):
            if not isinstance(session, dict):
                log.warning("prompt library migration: %s is not a JSON object; skipped", sp)
            continue
        remap = {}
        for v in st.get("versions") or []:
            if not (isinstance(v, dict) and _ID_RE.match(str(v.get("id") or "")) and isinstance(v.get("text"), str) and v["text"]):
                log.warning("prompt library migration: skipped a malformed version entry in %s", sp)
                continue
            digest = sha256(v["text"])
            same = next((x for x in lib["versions"] if x["sha256"] == digest), None)
            if same:
                remap[v["id"]] = same["id"]
                continue
            pid = f"P6.{lib['next']}" if any(x["id"] == v["id"] for x in lib["versions"]) else v["id"]
            remap[v["id"]] = pid
            lib["versions"].append({"id": pid, "parent": remap.get(v.get("parent"), v.get("parent")),
                                    "note": str(v.get("note") or ""), "created_at": v.get("created_at"),
                                    "sha256": digest, "text": v["text"]})
            _bump(lib, pid)
        try:
            _bump(lib, f"P6.{max(int(st.get('next', 2)) - 1, 1)}")
        except (TypeError, ValueError):
            pass
        st.pop("versions", None)
        st.pop("next", None)
        if st.get("active") in remap:
            st["active"] = remap[st["active"]]
        for a in st.get("adoptions") or []:
            if isinstance(a, dict):
                a["id"] = remap.get(a.get("id"), a.get("id"))
        plans.append((sp, session, [old for old, new in remap.items() if old != new]))
    _lib_write(lib)                                              # versions are safe before any session is touched
    for sp, session, drop in plans:
        for old in drop:
            for p in (sp.parent / "runs").glob(f"*/llm/*/{stage(old)}.json"):
                p.unlink(missing_ok=True)
        _write_json(sp, session)


def _builtin(pid):
    parent = None if pid == BASELINE else BASELINE
    note = ("Fixed baseline, text only (ported verbatim from FirstTest-prompts)" if pid == BASELINE
            else "Built-in multimodal prompt, derived from P5 in app/prompts.py")
    text = prompts.TEMPLATES[pid]
    return {"id": pid, "parent": parent, "note": note, "created_at": None, "builtin": True,
            "multimodal": pid != BASELINE, "sha256": sha256(text), "text": text}


def _all():
    """Every version, built-ins first, each with its text."""
    return [_builtin(BASELINE), _builtin(DEFAULT_ACTIVE)] + [
        dict(v, builtin=False, multimodal=True) for v in _lib()["versions"]]


def active_in(session):
    """The prompt id a session's review card uses (a session.json dict, open or not)."""
    a = (session.get("prompts") or {}).get("active")
    return a if a and any(v["id"] == a for v in _all()) else DEFAULT_ACTIVE


def active():
    """The open session's active prompt id; P6 when no campaign is open."""
    if config.DATA is None:
        return DEFAULT_ACTIVE
    return active_in(_load())


def stage(pid):
    """The cache stage a prompt's answers are stored under: score_p5, score_p6, score_p6.2, ..."""
    return "score_" + pid.lower()


def get(pid):
    for v in _all():
        if v["id"] == pid:
            return v
    raise KeyError(pid)


def text(pid):
    return get(pid)["text"]


def listing():
    """Every version without its text. The library is global; `active` and `adoptions` are the open
    session's (null when no campaign is open)."""
    s = _load() if config.DATA is not None else None
    act = active_in(s) if s is not None else None
    return {"active": act, "baseline": BASELINE,
            "versions": [dict({k: v for k, v in x.items() if k != "text"}, active=x["id"] == act,
                              chars=len(x["text"])) for x in _all()],
            "adoptions": (s.get("prompts") or {}).get("adoptions", []) if s is not None else None}


def validate(text_):
    """A prompt template must be non-empty and carry {video_summary} as its only placeholder.
    Literal braces must be doubled ({{ }}), exactly as in P5."""
    if not text_ or not text_.strip():
        raise ValueError("the prompt is empty")
    if len(text_) > MAX_CHARS:
        raise ValueError(f"the prompt is over {MAX_CHARS:,} characters")
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(text_) if f is not None]
    except ValueError as e:
        raise ValueError(f"unbalanced braces ({e}); write a literal brace as {{{{ or }}}}") from None
    other = sorted({f for f in fields if f != "video_summary"})
    if other:
        raise ValueError("only {video_summary} may be in braces; found " + ", ".join("{%s}" % f for f in other)
                         + " (write literal braces as {{ }})")
    if fields.count("video_summary") != 1:
        raise ValueError("the prompt must contain {video_summary} exactly once (it is where the video's data goes)")


def create(parent, text_, note=""):
    """Save a new version derived from `parent` (P6 or a custom version) in the global library.
    Versions never change once saved."""
    if parent == BASELINE:
        raise ValueError("P5 is the fixed baseline: derive new versions from P6 or a later version")
    text_ = (text_ or "").replace("\r\n", "\n")
    with _lock:
        try:
            par = get(parent)
        except KeyError:
            raise ValueError(f"unknown parent prompt {parent!r}") from None
        validate(text_)
        digest = sha256(text_)
        same = next((v["id"] for v in _all() if v["sha256"] == digest), None)
        if same:
            raise ValueError(f"this text is identical to {same}; change something before saving a version")
        lib = _lib()
        pid = f"P6.{lib['next']}"
        lib["next"] += 1                     # ids are never reused, even after a delete
        lib["versions"].append({"id": pid, "parent": par["id"], "note": (note or "").strip()[:500],
                                "created_at": _now(), "sha256": digest, "text": text_})
        _lib_write(lib)
    return get(pid)


def _active_in_sessions(pid):
    """The names of the saved sessions whose review card uses `pid`."""
    out = []
    for f in _sessions():
        try:
            s = json.loads((f / "session.json").read_text(encoding="utf-8"))
        except ValueError:
            continue
        if (s.get("prompts") or {}).get("active") == pid:
            out.append(s.get("name") or f.name)
    return out


def delete(pid):
    """Remove a custom version from the library, unless a session has it active or a run is in flight.
    The open session's cached answers for it are deleted; other sessions' caches and iteration records stay
    (their iterations show it as deleted)."""
    from . import jobs                       # local: jobs imports pipeline, which imports this module
    if not _ID_RE.match(pid or ""):
        raise ValueError(f"{pid} is built in and cannot be deleted")
    if jobs.running():
        raise Conflict("a run is in progress; wait for it to finish before deleting a version")
    with _lock:
        lib = _lib()
        if not any(v["id"] == pid for v in lib["versions"]):
            raise KeyError(pid)
        using = _active_in_sessions(pid)
        if using:
            raise Conflict(f"{pid} is the active prompt of {', '.join(using)}; adopt another one there first")
        lib["versions"] = [v for v in lib["versions"] if v["id"] != pid]
        _lib_write(lib)
    if config.RUNS is not None:
        for p in config.RUNS.glob(f"*/llm/*/{stage(pid)}.json"):
            p.unlink(missing_ok=True)


def set_active(pid, verdict=None, override_reason=None, iteration=None):
    """Make `pid` the prompt the review card uses.

    verdict is the gate's answer from iterations.adoption_check(): {"beats_baseline": bool, "why": str}.
    P6 (the built-in default) can always be restored. Anything else needs beats_baseline, or a
    written override reason, which is logged with the gate's answer.
    """
    s = _load()
    try:
        get(pid)
    except KeyError:
        raise ValueError(f"unknown prompt {pid!r}") from None
    if pid == BASELINE:
        raise ValueError("P5 is the baseline the versions are measured against, not a candidate")
    reason = (override_reason or "").strip()
    beats = bool(verdict and verdict.get("beats_baseline"))
    if pid != DEFAULT_ACTIVE and not beats:
        if len(reason) < MIN_REASON:
            raise Conflict(f"{pid} is not adopted: {(verdict or {}).get('why') or 'no iteration has compared it with P5'}. "
                           f"To adopt it anyway, give a written override reason (at least {MIN_REASON} characters).")
    st = _state(s)
    st["active"] = pid
    st["adoptions"].append({"id": pid, "at": _now(), "llm": config.llm_choice(), "iteration": iteration,
                            "beats_baseline": beats if pid != DEFAULT_ACTIVE else None,
                            "why": (verdict or {}).get("why"),
                            "override_reason": reason or None})
    _store(s)
    return listing()


def diff(pid, against=None):
    """Unified diff of a version against its parent (or another version)."""
    v = get(pid)
    base = get(against or v["parent"] or pid)
    return "".join(difflib.unified_diff(base["text"].splitlines(keepends=True), v["text"].splitlines(keepends=True),
                                        fromfile=base["id"], tofile=v["id"])) or "(no differences)\n"
