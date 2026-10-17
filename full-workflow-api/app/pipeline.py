"""Orchestrates steps 1-8 for one video and caches each stage as JSON under runs/<video_id>/.

Stages: preprocess.json -> features.json -> score_p6.json (+ score_p5.json for evaluation).
A stage is recomputed only when its file is missing or force=True.
"""

import json

from . import flatten as fl
from . import config, fetch, guard, ingest, media, scoring, vision
from .config import NA
from .ingest import num, val


def _path(video_id, stage):
    return config.RUNS / video_id / f"{stage}.json"


def load(video_id, stage):
    p = _path(video_id, stage)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _save(video_id, stage, data):
    p = _path(video_id, stage)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _row(video_id):
    videos, comments = ingest.load_inputs()
    if video_id not in videos:
        raise KeyError(video_id)
    return videos[video_id], comments.get(video_id)


def summary(video_id, multimodal):
    """flatten() output: six sections for P5, seven for P6."""
    row, comments = _row(video_id)
    content = None
    if multimodal:
        content = {"preprocess": load(video_id, "preprocess"), "features": load(video_id, "features")}
    return fl.flatten(row, comments, content)


def run_p5(video_id, force=False):
    row, _ = _row(video_id)
    cached = None if force else load(video_id, "score_p5")
    return cached or _save(video_id, "score_p5", scoring.score("P5", summary(video_id, False), row))


def fetch_clip(video_id):
    """Download the video from its URL when it is not on disk yet. True if it downloaded."""
    url = ingest.video_url(video_id)
    if not url or ingest.clip_path(video_id):
        return False
    fetch.download(url, ingest.downloaded_clip(video_id))
    ingest.mark_downloaded(video_id, url)
    return True


def process(video_id, force=False, progress=None):
    """Fetch -> preprocess -> visual analysis -> merge -> P6 -> rule guard. Returns the card.

    `progress` is called with the name of each step as it starts.
    """
    step = progress or (lambda name: None)
    row, _ = _row(video_id)

    step("downloading")
    if fetch_clip(video_id):
        force = True                # a new video makes every cached stage stale

    pre = None if force else load(video_id, "preprocess")
    if pre is None:
        step("preprocessing")
        pre = _save(video_id, "preprocess", media.preprocess(video_id, row))

    feats = None if force else load(video_id, "features")
    if feats is None:
        step("analysing")
        feats = _save(video_id, "features", vision.analyse(video_id, row, pre) or {})

    if force or load(video_id, "score_p6") is None:
        step("scoring")
        _save(video_id, "score_p6", scoring.score("P6", summary(video_id, True), row))
    return card(video_id)


def status(video_id):
    return {
        "has_clip": ingest.clip_path(video_id) is not None,
        "source_url": ingest.video_url(video_id),
        "preprocessed": _path(video_id, "preprocess").exists(),
        "analysed": _path(video_id, "features").exists(),
        "scored": _path(video_id, "score_p6").exists(),
    }


def _spoken_summary(row, s):
    """Plain sentences for text-to-speech: no symbols, no markup."""
    if not s:
        return ""
    parts = [f"Video {row['video_id']} for {row.get('brand') or 'the brand'}.",
             f"Recommendation: {s['verdict'].replace('_', ' ').lower()}."]
    if s.get("score") is not None:
        parts.append(f"Score {s['score']} out of 100, plausible range {s['score_low']} to "
                     f"{s['score_high']}, confidence {(s.get('confidence') or 'unknown').lower()}.")
    if s.get("rule_override"):
        parts.append("A data rule overrode the model: " + "; ".join(s["rule_hits"]) + ".")
    for i, r in enumerate(s.get("reasons", []), 1):
        parts.append(f"Reason {i}. {r}")
    return " ".join(parts)


def _frame_url(video_id, file):
    """Frame files keep their names when a video is replaced, so the URL carries the file's
    mtime: otherwise the browser shows the previous video's frames from its cache."""
    if not file:
        return None
    p = config.RUNS / video_id / "frames" / file
    v = int(p.stat().st_mtime) if p.exists() else 0
    return f"/artifacts/{video_id}/frames/{file}?v={v}"


def card(video_id):
    """Everything the review card renders, in one payload."""
    row, comments = _row(video_id)
    pre = load(video_id, "preprocess") or {}
    feats = load(video_id, "features") or {}
    s = load(video_id, "score_p6")
    d = fl.derived_metrics(row)

    clip = ingest.clip_path(video_id)
    comment_texts = [] if comments is None else [str(t) for t in comments["text"].tolist()]
    frames = feats.get("frames") or pre.get("frames") or []
    flags = guard.find_flags(pre.get("transcript"), frames, comment_texts)

    return {
        "video_id": video_id,
        "status": status(video_id),
        "caption": row.get("caption"),
        "brand": row.get("brand"),
        "brand_category": row.get("brand_category"),
        "creator": row.get("handle"),
        "hours_since_post": num(row.get("hours_since_post")),
        # ?v= so the browser does not replay a cached clip after the URL changes
        "video_url": f"/api/videos/{video_id}/clip?v={int(clip.stat().st_mtime)}" if clip else None,
        "captions_url": f"/api/videos/{video_id}/captions.vtt" if pre.get("transcript") else None,
        "media_source": pre.get("source"),
        "duration_s": pre.get("duration_s"),
        "language": pre.get("language"),
        "frames": [
            {"t_s": f.get("t_s"), "frame_id": f.get("frame_id"),
             "note": val(f.get("note")) or val(f.get("scene_description")),
             "on_screen_text": val(f.get("on_screen_text")),
             "flagged": guard.scan(f.get("on_screen_text")),
             "url": _frame_url(video_id, f.get("file"))}
            for f in frames
        ],
        "transcript": pre.get("transcript"),
        "features": {k: v for k, v in feats.items() if k not in ("frames", "_usage")} or None,
        "metrics": {
            "raw": {k: num(row.get(k)) for k in
                    ("reach", "views", "likes", "comments", "shares", "saved", "total_interactions")},
            "rates": d,
            "skip_rate_pct": num(row.get("skip_rate_pct")),
            "avg_watch_time_s": None if num(row.get("avg_watch_time_ms")) is None
                                else num(row.get("avg_watch_time_ms")) / 1000,
        },
        "score": s,
        "injection_flags": flags,
        "spoken_summary": _spoken_summary(row, s),
        "text_summary": summary(video_id, False),      # the "without the card" view
        "na": NA,
    }


def captions_vtt(video_id):
    """The transcript as WebVTT, for the player's captions track."""
    def ts(t):
        return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}"

    pre = load(video_id, "preprocess") or {}
    out = ["WEBVTT", ""]
    for s in pre.get("transcript") or []:
        out += [f"{ts(s['start_s'])} --> {ts(max(s['end_s'], s['start_s'] + 0.5))}", s["text"], ""]
    return "\n".join(out)
