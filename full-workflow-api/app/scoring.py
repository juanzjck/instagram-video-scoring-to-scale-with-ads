"""Step 6 - score a video with P5 (text only) or P6 (multimodal), then apply the rule guard.

The model proposes; the rules dispose. apply_rules() runs in code after the model answers,
so a video under 48h old, with tiny reach or with missing metrics ends as DEFER_TO_HUMAN
whatever the model - or the video's own on-screen text - said.
"""

import re

from . import llm, prompts
from .config import CORE_METRICS, MIN_HOURS, MIN_REACH
from .ingest import num

VERDICTS = ["SCALE", "DO_NOT_SCALE", "DEFER_TO_HUMAN", "FLAG_AUTHENTICITY"]

_VERDICT_RE = re.compile(
    r"^\s*\**\s*VERDICT\s*\**\s*:\s*\**\s*"
    r"(SCALE|DO[_ ]NOT[_ ]SCALE|DEFER[_ ]TO[_ ]HUMAN|FLAG[_ ]AUTHENTICITY|FLAG[_ ]FRAUD)",
    re.IGNORECASE | re.MULTILINE)
_SCORE_RE = re.compile(r"^\s*\**\s*SCORE\s*\**\s*:\s*\**\s*(\d{1,3})", re.IGNORECASE | re.MULTILINE)
_RANGE_RE = re.compile(r"^\s*\**\s*SCORE_RANGE\s*\**\s*:\s*\**\s*(\d{1,3})\s*[-–to]+\s*(\d{1,3})",
                       re.IGNORECASE | re.MULTILINE)
_CONF_RE = re.compile(r"^\s*\**\s*CONFIDENCE\s*\**\s*:\s*\**\s*(LOW|MEDIUM|HIGH)",
                      re.IGNORECASE | re.MULTILINE)
_REASON_RE = re.compile(r"^\s*\d+\.\s+(.+?)(?=^\s*\d+\.\s|\Z)", re.MULTILINE | re.DOTALL)
_TS_RE = re.compile(r"\b\d{1,2}:\d{2}\b|\bkf\d{2}\b|\bt=\d+s\b")

# Used only when the model gives no SCORE_RANGE (P5 has no such line).
_BAND = {"HIGH": 6, "MEDIUM": 12, "LOW": 20}


def normalise_verdict(s):
    """Canonicalise a verdict token. FLAG_FRAUD (the answer key's spelling) maps onto
    FLAG_AUTHENTICITY (the prompts' spelling)."""
    if s is None:
        return None
    t = re.sub(r"[\s\-]+", "_", str(s).strip().upper())
    if t == "FLAG_FRAUD":
        t = "FLAG_AUTHENTICITY"
    return t if t in VERDICTS else None


def parse(text):
    """Pull the structured fields out of a P5/P6 response."""
    m = _VERDICT_RE.search(text)
    s = _SCORE_RE.search(text)
    r = _RANGE_RE.search(text)
    c = _CONF_RE.search(text)
    score = min(100, int(s.group(1))) if s else None
    conf = c.group(1).upper() if c else None
    body = text.split("REASONS", 1)[1] if "REASONS" in text else ""
    reasons = [re.sub(r"\s+", " ", x.replace("**", "")).strip() for x in _REASON_RE.findall(body)]

    if r:
        lo, hi = sorted((min(100, int(r.group(1))), min(100, int(r.group(2)))))
        range_source = "model"
    elif score is not None:
        band = _BAND.get(conf, 20)
        lo, hi = max(0, score - band), min(100, score + band)
        range_source = "derived from confidence"
    else:
        lo = hi = range_source = None

    return {
        "verdict": normalise_verdict(m.group(1)) if m else None,
        "score": score,
        "score_low": lo,
        "score_high": hi,
        "range_source": range_source,
        "confidence": conf,
        "reasons": reasons,
        "reasons_citing_time": sum(bool(_TS_RE.search(x)) for x in reasons),
    }


def rule_check(row):
    """The reasons, if any, this video must be deferred regardless of the model."""
    hits = []
    hrs = num(row.get("hours_since_post"))
    if hrs is None:
        hits.append("hours since posting is not available")
    elif hrs < MIN_HOURS:
        hits.append(f"posted {hrs:.0f}h ago, under the {MIN_HOURS}h insight-lag window")
    missing = [m for m in CORE_METRICS if num(row.get(m)) is None]
    if missing:
        hits.append("metrics not available: " + ", ".join(missing))
    reach = num(row.get("reach"))
    if reach is not None and reach < MIN_REACH:
        hits.append(f"reach {reach:,.0f} is under the {MIN_REACH:,} floor")
    return hits


def apply_rules(parsed, row):
    """Final verdict = DEFER_TO_HUMAN when a rule fires, else the model's verdict."""
    hits = rule_check(row)
    model_verdict = parsed["verdict"]
    out = dict(parsed, model_verdict=model_verdict, rule_hits=hits, rule_override=False)
    if hits and model_verdict != "DEFER_TO_HUMAN":
        out.update(verdict="DEFER_TO_HUMAN", confidence="LOW", rule_override=True)
    elif model_verdict is None:
        out.update(verdict="DEFER_TO_HUMAN", confidence="LOW")   # unparseable -> a human looks
    return out


def score(prompt_id, summary, row):
    """Run one prompt over one summary and return parsed + rule-guarded output."""
    resp = llm.complete(prompts.render(prompt_id, summary), max_tokens=1200)
    out = apply_rules(parse(resp["text"]), row)
    out.update(prompt_id=prompt_id, raw_output=resp["text"],
               usage={k: resp[k] for k in ("input_tokens", "output_tokens", "latency_s", "model")})
    return out
