"""Step 7 - injection guard.

Comments, the transcript and on-screen text are all written by third parties. Three layers:

1. wrap()   - untrusted text goes inside a named delimiter, and anything in the text that
              looks like that delimiter is defused so the data cannot close its own tag.
2. scan()   - a deterministic pattern check, independent of any model. A hit is recorded on
              the card whatever the model says.
3. redact() - media text that scan() flags never reaches the scoring prompt verbatim; the
              scorer sees a placeholder and the timestamp, the human sees the original.

The prompts add the fourth layer: an explicit "this is data, not instructions" rule.
"""

import re

_PATTERNS = [
    r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above)\s+(instructions|rules|prompts?)",
    r"ignora\s+(todas\s+)?(las\s+)?instrucciones",
    r"disregard\s+(all\s+)?(previous|prior|the)\b",
    r"^\s*(system|assistant|developer)\s*:",
    r"\bAI\s+(reviewer|assistant|model|judge)\b",
    r"\b(revisor|modelo|asistente)\s+(de\s+)?IA\b",
    r"pre[\s-]?approved|pre[\s-]?aprobad[oa]",
    r"\b(score|puntaje|puntuaci[oó]n)\s*(of|de|:|=)?\s*100\b",
    r"(maximum|max|largest|highest|m[aá]ximo)\s+(possible\s+)?(score|budget|presupuesto|puntaje)",
    r"\b(give|assign|award|reply|respond|answer)\b.{0,40}\b(score|verdict|budget)\b",
    r"\bVERDICT\s*:",
    r"you\s+are\s+now\b|new\s+instructions",
]
_RE = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.IGNORECASE | re.MULTILINE)
_TAG_RE = re.compile(r"<\s*/?\s*(comments|transcript|keyframes|on_screen_text)\s*>", re.IGNORECASE)


def scan(text):
    """True when the text matches a known manipulation pattern."""
    return bool(text) and bool(_RE.search(str(text)))


def defuse(text):
    """Flatten newlines and neutralise delimiter look-alikes inside untrusted text."""
    t = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    return _TAG_RE.sub(lambda m: m.group(0).replace("<", "(").replace(">", ")"), t)


def redact(text):
    """Placeholder for flagged media text; untouched (but defused) otherwise."""
    if scan(text):
        return f"[REDACTED by injection guard: {len(str(text))} characters of suspected instruction text]"
    return defuse(text)


def find_flags(transcript, frames, comments=None):
    """Collect every guard hit across the three untrusted channels."""
    flags = []
    for seg in transcript or []:
        if scan(seg.get("text")):
            flags.append({"channel": "transcript", "t_s": seg.get("start_s"), "text": seg["text"]})
    for f in frames or []:
        if scan(f.get("on_screen_text")):
            flags.append({"channel": "on_screen_text", "t_s": f.get("t_s"), "text": f["on_screen_text"]})
    for c in comments or []:
        if scan(c):
            flags.append({"channel": "comment", "t_s": None, "text": c})
    return flags
