"""Prompt versions: the tuning loop of the Section 6 workflow design.

After a campaign, the data/AI team reads the error report and hand-tunes P6. Each tuned prompt is a
new, immutable version (P6.2, P6.3, ...) kept in the open session's session.json under "prompts".
A version is only adopted as the one the review card uses when an iteration shows it beats the P5
baseline (app/iterations.py); an override needs a written reason and is logged.

Built in, never edited here: P5 (the fixed text-only baseline) and P6 (derived from P5 in prompts.py).
Every custom version is a P6-family prompt: it gets the multimodal summary (CONTENT FEATURES included).
"""

import difflib
import hashlib
import json
import re
import string
from datetime import datetime, timezone

from . import config, prompts

BASELINE = "P5"
DEFAULT_ACTIVE = "P6"
MAX_CHARS = 100_000
MIN_REASON = 10                  # characters an adoption override reason needs
_ID_RE = re.compile(r"^P6\.\d+$")


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


def _store(session):
    p = _session_path()
    session["updated_at"] = _now()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _state(session):
    return session.setdefault("prompts", {"versions": [], "next": 2, "adoptions": []})


def _builtin(pid):
    parent = None if pid == BASELINE else BASELINE
    note = ("Fixed baseline, text only (ported verbatim from FirstTest-prompts)" if pid == BASELINE
            else "Built-in multimodal prompt, derived from P5 in app/prompts.py")
    text = prompts.TEMPLATES[pid]
    return {"id": pid, "parent": parent, "note": note, "created_at": None, "builtin": True,
            "multimodal": pid != BASELINE, "sha256": sha256(text), "text": text}


def _all(session=None):
    """Every version, built-ins first, each with its text."""
    s = _load() if session is None else session
    return [_builtin(BASELINE), _builtin(DEFAULT_ACTIVE)] + [
        dict(v, builtin=False, multimodal=True) for v in (s.get("prompts") or {}).get("versions", [])]


def active_in(session):
    """The prompt id a session's review card uses (a session.json dict, open or not)."""
    a = (session.get("prompts") or {}).get("active")
    return a if a and any(v["id"] == a for v in _all(session)) else DEFAULT_ACTIVE


def active():
    """The open session's active prompt id; P6 when no campaign is open."""
    if config.DATA is None:
        return DEFAULT_ACTIVE
    return active_in(_load())


def stage(pid):
    """The cache stage a prompt's answers are stored under: score_p5, score_p6, score_p6.2, ..."""
    return "score_" + pid.lower()


def get(pid, session=None):
    for v in _all(session):
        if v["id"] == pid:
            return v
    raise KeyError(pid)


def text(pid):
    return get(pid)["text"]


def listing():
    """Every version without its text, plus the active id and the adoption log."""
    s = _load()
    act = active_in(s)
    return {"active": act, "baseline": BASELINE,
            "versions": [dict({k: v for k, v in x.items() if k != "text"}, active=x["id"] == act,
                              chars=len(x["text"])) for x in _all(s)],
            "adoptions": (s.get("prompts") or {}).get("adoptions", [])}


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
    """Save a new version derived from `parent` (P6 or a custom version). Versions never change once saved."""
    s = _load()
    if parent == BASELINE:
        raise ValueError("P5 is the fixed baseline: derive new versions from P6 or a later version")
    try:
        par = get(parent, s)
    except KeyError:
        raise ValueError(f"unknown parent prompt {parent!r}") from None
    text_ = (text_ or "").replace("\r\n", "\n")
    validate(text_)
    digest = sha256(text_)
    same = next((v["id"] for v in _all(s) if v["sha256"] == digest), None)
    if same:
        raise ValueError(f"this text is identical to {same}; change something before saving a version")
    st = _state(s)
    pid = f"P6.{st['next']}"
    st["next"] += 1                          # ids are never reused, even after a delete
    st["versions"].append({"id": pid, "parent": par["id"], "note": (note or "").strip()[:500],
                           "created_at": _now(), "sha256": digest, "text": text_})
    _store(s)
    return get(pid)


def delete(pid):
    """Remove a custom version that is not active. Its cached answers are deleted; iteration records stay."""
    if not _ID_RE.match(pid or ""):
        raise ValueError(f"{pid} is built in and cannot be deleted")
    s = _load()
    st = _state(s)
    if not any(v["id"] == pid for v in st["versions"]):
        raise KeyError(pid)
    if active_in(s) == pid:
        raise Conflict(f"{pid} is the active prompt; adopt another one first")
    st["versions"] = [v for v in st["versions"] if v["id"] != pid]
    _store(s)
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
        get(pid, s)
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
