"""Sessions and the datasets built in them.

A session is one campaign, in one folder, uploads/<id>/. Its memory is session.json: the name, timestamps and
every row typed in the Dataset form, saved as the user types. Generating the dataset validates
those rows and writes the CSVs next to it; the pipeline's results go in its runs/. The app
starts with no session open; any saved session can be reopened, form and results included.

SCHEMA mirrors the data-template/ files a user supplies (same column order). Not in the form:
media, keyframes and transcripts, which the pipeline produces from each video's clip; the answer
keys (ground_truth*.csv), added later with save_answer_keys() and the Ground truth form; and
manual_review.csv, the blind human decisions, entered per reviewer in the Manual review form.
"""

import csv
import hashlib
import io
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import PurePath

import pandas as pd

from . import config, flatten, ingest, jobs, media, pipeline

_SAFE_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")     # video ids become folder names
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_CLIP_BYTES = 500 * 1024 * 1024
SESSION_FILE = "session.json"


def _c(name, label, kind="text", required=False, hint="", options=None, suggest=None, auto=False):
    """One form column. kind: id, ref, text, long, int, float, datetime, enum, json.
    auto columns are filled in by the server and not shown in the form."""
    return {"name": name, "label": label, "kind": kind, "required": required, "hint": hint,
            "options": options, "suggest": suggest, "auto": auto}


LANGS = ["es", "en"]

SCHEMA = {
    "campaigns.csv": {"title": "Campaign", "one_row_per": "session (one campaign)", "single": True, "cols": [
        _c("campaign_id", "Campaign ID", "id", True, "e.g. cmp_103"),
        _c("brand", "Brand", required=True, hint="e.g. FitApp Pro"),
        _c("brand_category", "Brand category", hint="e.g. fitness"),
        _c("product_type", "Product type", suggest=["ecommerce", "subscription_app"],
           hint="any text, e.g. ecommerce, subscription_app"),
        _c("target_cac", "Target CAC (USD)", "float", hint="max cost per conversion the brand accepts"),
        _c("target_countries", "Target countries", hint="ISO codes separated by | e.g. CO|EC"),
        _c("campaign_clicks", "Campaign clicks", "int"),
        _c("campaign_views", "Campaign views", "int"),
        _c("click_ratio", "Click ratio", "float", hint="blank = clicks ÷ views"),
    ]},
    "creators.csv": {"title": "Creators", "one_row_per": "creator", "cols": [
        _c("creator_id", "Creator ID", "id", True, "e.g. cr_11"),
        _c("handle", "Handle", hint="without @"),
        _c("niche", "Niche", hint="e.g. beauty"),
        _c("followers", "Followers", "int"),
        _c("primary_language", "Primary language", "enum", options=LANGS),
        _c("follower_demographics_country", "Followers by country", "json",
           hint='JSON, e.g. {"CO": 26400, "EC": 7100}'),
        _c("link_clicks", "Link clicks", "int",
           hint="optional: clicks from this creator's code or link in this campaign, all their videos"),
    ]},
    "videos.csv": {"title": "Videos", "one_row_per": "video (the early signals the model sees)", "cols": [
        _c("video_id", "Video ID", "id", True, "e.g. vid_011"),
        _c("creator_id", "Creator", "ref", True, options="creators.csv"),
        _c("campaign_id", "Campaign", "ref", True, options="campaigns.csv", auto=True),   # the session's campaign
        _c("media_product_type", "Media type", suggest=["REELS", "VIDEO"]),
        _c("posted_at", "Posted at (UTC)", "datetime", hint="2026-08-28T14:10:00Z"),
        _c("collected_at", "Metrics read at (UTC)", "datetime", hint="2026-08-31T14:10:00Z"),
        _c("hours_since_post", "Hours since post", "float",
           hint="blank = collected − posted; under 48 defers to a human"),
        _c("caption", "Caption", "long"),
        _c("reach", "Reach", "int", hint="under 1,000 defers to a human"),
        _c("views", "Views", "int"),
        _c("likes", "Likes", "int"),
        _c("comments", "Comments", "int"),
        _c("shares", "Shares", "int"),
        _c("saved", "Saved", "int"),
        _c("total_interactions", "Total interactions", "int",
           hint="blank = likes + comments + shares + saved"),
        _c("avg_watch_time_ms", "Avg watch time (ms)", "int"),
        _c("skip_rate_pct", "Skip rate (%)", "float", hint="0–100"),
        _c("video_url", "Video URL", hint="optional: mp4 link, TikTok / IG / YouTube page, or upload a file"),
    ]},
    "comments.csv": {"title": "Comments", "one_row_per": "comment", "cols": [
        _c("comment_id", "Comment ID", auto=True),
        _c("video_id", "Video", "ref", True, options="videos.csv"),
        _c("text", "Text", "long", True),
        _c("timestamp", "Posted at (UTC)", "datetime"),
        _c("username", "Username"),
        _c("like_count", "Likes", "int"),
    ]},
}
REQUIRED_FILES = ["campaigns.csv", "creators.csv", "videos.csv"]
ANSWER_KEYS = {    # headers as in data-template/
    "ground_truth.csv": ["video_id", "scenario", "expected_verdict", "performed_well_after_scaling",
                         "ad_spend", "conversions", "cac", "roas", "notes"],
    "ground_truth_media.csv": ["video_id", "expected_hook_strength", "expected_product_first_seen_s",
                               "expected_brand_match", "expected_cta_present", "expected_face_on_screen",
                               "expected_injection_in_media", "expected_safety_issue",
                               "multimodal_expected_verdict", "notes"],
}


# The real results of each video, entered per campaign in the Evaluation tab and then locked.
# Columns and order as data-template/ground_truth.csv.
SCENARIOS = ["clear_winner", "clear_loser", "borderline", "trap_high_engagement", "trap_quiet_seller",
             "too_new", "tiny_reach", "missing_metrics", "inorganic", "audience_mismatch"]
GROUND_TRUTH = [
    _c("video_id", "Video", "ref", True, auto=True),
    _c("scenario", "Scenario", suggest=SCENARIOS, hint="label for your analysis"),
    _c("expected_verdict", "Expected verdict", "enum", hint="the right call, knowing the real results",
       options=["SCALE", "DO_NOT_SCALE", "DEFER_TO_HUMAN", "FLAG_FRAUD"]),
    _c("performed_well_after_scaling", "Performed well after scaling", "enum", options=["yes", "no"],
       hint="blank = yes when CAC ≤ the campaign's target CAC; blank if never scaled"),
    _c("ad_spend", "Ad spend (USD)", "float", hint="spent scaling it"),
    _c("conversions", "Conversions", "int", hint="sales or sign-ups"),
    _c("cac", "CAC (USD)", "float", hint="blank = ad spend ÷ conversions"),
    _c("roas", "ROAS", "float", hint="optional"),
    _c("notes", "Notes", "long", hint="why"),
]
assert [c["name"] for c in GROUND_TRUTH] == ANSWER_KEYS["ground_truth.csv"]

# Blind human decisions, the baseline the AI is measured against: made from the videos.csv
# signals only, before anyone sees a score. One sheet per reviewer, frozen once submitted.
# Columns and order as data-template/manual_review.csv.
MANUAL_REVIEW_FILE = "manual_review.csv"
MANUAL_REVIEW = [
    _c("video_id", "Video", "ref", True, auto=True),
    _c("reviewer", "Reviewer", required=True, auto=True),
    _c("decision", "Decision", "enum", hint="from the signals shown only", options=["SCALE", "DO_NOT_SCALE"]),
    _c("reason", "Reason", "long", hint="one sentence: the evidence you used"),
    _c("minutes_spent", "Minutes spent", "float", hint="blank = timed from the row"),
]
# What a blind reviewer sees of each video: videos.csv and the creator, nothing the pipeline produced.
BLIND_FIELDS = ["handle", "followers", "media_product_type", "hours_since_post", "caption", "reach", "views",
                "likes", "comments", "shares", "saved", "total_interactions", "avg_watch_time_ms",
                "skip_rate_pct", "video_url"]


class GroundTruthLocked(ValueError):
    pass


class ReviewSubmitted(GroundTruthLocked):     # same handling: the sheet can no longer change (409)
    pass


class DatasetError(ValueError):
    """The form data is not usable. `errors` is [{file, row, column, message}]; row is 0-based."""

    def __init__(self, errors):
        self.errors = errors
        first = errors[0]
        where = f"{first['file']}" + (f" row {first['row'] + 1}" if first.get("row") is not None else "")
        super().__init__(f"{len(errors)} problem(s); first: {where}: {first['message']}")


def schema():
    return {"files": SCHEMA, "required": REQUIRED_FILES}


def _num_text(x):
    return str(int(x)) if float(x).is_integer() else repr(float(x))


def _parse_time(s):
    t = datetime.fromisoformat(s.strip().replace(" ", "T"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)            # the form asks for UTC
    return t.astimezone(timezone.utc)


def _clean_cell(col, raw):
    """Normalised text for one cell, or "" when blank. Raises ValueError with a readable reason."""
    s = "" if raw is None else str(raw).strip()
    if not s:
        return ""
    kind = col["kind"]
    if kind in ("id", "ref"):
        if not _SAFE_ID.match(s):
            raise ValueError("only letters, digits, _ . - are allowed")
    elif kind == "int":
        try:
            x = float(s.replace(",", "").replace("_", ""))
        except ValueError:
            raise ValueError("must be a whole number") from None
        if not x.is_integer() or x < 0:
            raise ValueError("must be a whole number, 0 or more")
        return str(int(x))
    elif kind == "float":
        try:
            x = float(s)
        except ValueError:
            raise ValueError("must be a number") from None
        if x < 0:
            raise ValueError("must be 0 or more")
        if col["name"] == "skip_rate_pct" and x > 100:
            raise ValueError("is a percentage, 0–100")
        return _num_text(x)
    elif kind == "datetime":
        try:
            return _parse_time(s).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise ValueError("must be a date-time like 2026-08-28T14:10:00Z") from None
    elif kind == "enum":
        match = [o for o in col["options"] if o.lower() == s.lower()]
        if not match:
            raise ValueError(f"must be one of: {', '.join(col['options'])}")
        return match[0]
    elif kind == "json":
        try:
            d = json.loads(s)
        except ValueError:
            raise ValueError('must be JSON, e.g. {"CO": 26400}') from None
        if not isinstance(d, dict) or not all(isinstance(v, (int, float)) for v in d.values()):
            raise ValueError('must map country codes to numbers, e.g. {"CO": 26400}')
        return json.dumps(d, ensure_ascii=False)
    elif col["name"] == "target_countries":
        return "|".join(p.strip().upper() for p in re.split(r"[|,;]", s) if p.strip())
    return s


def _derive(name, row):
    """Fill the columns the template defines as arithmetic on other columns, when left blank."""
    def f(k):
        return float(row[k]) if row.get(k) else None

    if name == "campaigns.csv" and not row["click_ratio"] and f("campaign_clicks") is not None and f("campaign_views"):
        row["click_ratio"] = _num_text(round(f("campaign_clicks") / f("campaign_views"), 4))
    if name == "videos.csv":
        if not row["hours_since_post"] and row["posted_at"] and row["collected_at"]:
            hrs = (_parse_time(row["collected_at"]) - _parse_time(row["posted_at"])).total_seconds() / 3600
            if hrs >= 0:
                row["hours_since_post"] = _num_text(round(hrs, 1))
        parts = [f(k) for k in ("likes", "comments", "shares", "saved")]
        if not row["total_interactions"] and None not in parts:      # blank stays blank, never a guess
            row["total_interactions"] = _num_text(sum(parts))


def _fill_auto(name, rows):
    seen = {}
    for r in rows:
        vid = r.get("video_id")
        n = seen[vid] = seen.get(vid, -1) + 1
        if name == "comments.csv" and not r["comment_id"]:
            r["comment_id"] = f"{vid}_cmt{n:02d}"


def clean(tables):
    """tables: {file name: [ {column: value} ]} from the form. Returns the same shape, normalised.
    Raises DatasetError listing every problem, so the form can mark all of them at once."""
    errors, out = [], {}

    def err(file, row, column, message):
        errors.append({"file": file, "row": row, "column": column, "message": message})

    for name in tables:
        if name not in SCHEMA:
            err(name, None, None, "unknown table")
    for name, spec in SCHEMA.items():
        rows = []
        for i, raw in enumerate(tables.get(name) or []):
            if not isinstance(raw, dict) or not any(str(raw.get(c["name"]) or "").strip()
                                                    for c in spec["cols"] if not c["auto"]):
                continue                                          # an untouched blank row
            r = {"_row": i}
            for c in spec["cols"]:
                try:
                    r[c["name"]] = "" if c["auto"] else _clean_cell(c, raw.get(c["name"]))
                except ValueError as e:
                    r[c["name"]] = ""
                    err(name, i, c["name"], f"{c['label']} {e}")
                    continue
                if c["required"] and not c["auto"] and not r[c["name"]]:
                    err(name, i, c["name"], f"{c['label']} is required")
            rows.append(r)
        out[name] = rows

    for name in REQUIRED_FILES:
        if not out[name]:
            err(name, None, None, f"add at least one row to {SCHEMA[name]['title']}")
    if len(out["campaigns.csv"]) > 1:
        err("campaigns.csv", None, None, "a session holds one campaign: start a new session for each other campaign")
    campaign_id = out["campaigns.csv"][0]["campaign_id"] if out["campaigns.csv"] else ""
    for r in out["videos.csv"]:
        r["campaign_id"] = campaign_id       # every video belongs to the session's campaign

    def unique(name, key, label):
        seen = {}
        for r in out[name]:
            k = tuple(r[c] for c in key)
            if all(k) and k in seen:
                err(name, r["_row"], key[-1], f"duplicate {label} (also in row {seen[k] + 1})")
            seen.setdefault(k, r["_row"])

    unique("campaigns.csv", ["campaign_id"], "Campaign ID")
    unique("creators.csv", ["creator_id"], "Creator ID")
    unique("videos.csv", ["video_id"], "Video ID")
    for r in out["campaigns.csv"]:
        if r["campaign_id"] and not config.DATASET_ID_RE.match(r["campaign_id"]):
            err("campaigns.csv", r["_row"], "campaign_id", "Campaign ID must start with a letter or digit "
                "and use only letters, digits, _ - (it names the recap folder)")

    ids = {n: {r[c] for r in out[n]} for n, c in
           [("campaigns.csv", "campaign_id"), ("creators.csv", "creator_id"), ("videos.csv", "video_id")]}
    for name, spec in SCHEMA.items():
        for c in spec["cols"]:
            if c["kind"] != "ref":
                continue
            for r in out[name]:
                if r[c["name"]] and r[c["name"]] not in ids[c["options"]]:
                    err(name, r["_row"], c["name"],
                        f"{c['label']} {r[c['name']]!r} is not in {SCHEMA[c['options']]['title']}")
    if errors:
        raise DatasetError(errors)

    for name, rows in out.items():
        for r in rows:
            _derive(name, r)
        _fill_auto(name, rows)
    return out


def _write_csv(path, header, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _activate(dataset_id):
    config.activate(dataset_id)
    ingest.load_inputs.cache_clear()
    jobs.reset()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sanitize_form(tables):
    """Only the form's own tables and columns, as text, so session.json stays a clean record."""
    out = {}
    for name, spec in SCHEMA.items():
        cols = [c["name"] for c in spec["cols"] if not c["auto"]]
        out[name] = [{c: "" if r.get(c) is None else str(r.get(c)) for c in cols}
                     for r in (tables or {}).get(name) or [] if isinstance(r, dict)]
    return out


def _form_from_csvs(folder):
    """The form rows of a dataset folder saved before sessions had a memory file."""
    tables = {}
    for name in SCHEMA:
        p = folder / name
        tables[name] = (pd.read_csv(p, dtype=str, keep_default_na=False).to_dict("records")
                        if p.exists() else [])
    return _sanitize_form(tables)


def _load_session(folder):
    p = folder / SESSION_FILE
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    generated = (folder / "videos.csv").exists()
    form = _form_from_csvs(folder)
    return {"id": folder.name, "name": campaign_name(form) or f"Dataset {folder.name}", "created_at": None, "updated_at": None,
            "generated_at": None, "generated": generated, "form": form}


def _save_session(session):
    session["updated_at"] = _now()
    p = config.UPLOADS / session["id"] / SESSION_FILE
    tmp = p.with_name(SESSION_FILE + ".tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)                           # never a half-written memory file
    return session


def _filled(rows):
    return sum(1 for r in rows or [] if any(str(v).strip() for v in r.values()))


def campaign_name(form):
    """The session's name: its campaign's brand and id."""
    r = ((form or {}).get("campaigns.csv") or [{}])[0]
    return " · ".join(x for x in (str(r.get("brand") or "").strip(), str(r.get("campaign_id") or "").strip()) if x) or None


def _summary(session, details=False):
    form = session.get("form", {})
    out = {k: session.get(k) for k in ("id", "name", "created_at", "updated_at", "generated_at")} | {
        "generated": bool(session.get("generated_at") or session.get("generated")),
        "videos_in_form": _filled(form.get("videos.csv"))}
    if details:                              # what the session list shows when a row is expanded
        folder = config.UPLOADS / session["id"]
        runs = folder / "runs"
        log = runs / "decisions.jsonl"
        out |= {"rows": {name: _filled(form.get(name)) for name in SCHEMA},
                "scored": len(list(runs.glob("*/score_p6.json"))) if runs.is_dir() else 0,
                "decisions": sum(1 for l in log.read_text(encoding="utf-8").splitlines() if l.strip())
                             if log.exists() else 0,
                "recaps": len(list((runs / "recaps").glob("*/recap.mp4"))) if runs.is_dir() else 0,
                "answer_keys": [n for n in ANSWER_KEYS if (folder / n).exists()],
                "ground_truth": {"locked_at": (session.get("ground_truth") or {}).get("locked_at"),
                                 "draft_rows": sum(1 for r in ((session.get("ground_truth") or {}).get("rows") or {}).values()
                                                   if any(str(v).strip() for v in r.values()))},
                "manual_review": {name: {"submitted_at": r.get("submitted_at"), "rows": _filled(r.get("rows", {}).values())}
                                  for name, r in (session.get("manual_review") or {}).items()},
                "campaign": ((form.get("campaigns.csv") or [{}])[0])}
    return out


def _new_session(name=None):
    config.UPLOADS.mkdir(exist_ok=True)
    session_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    (config.UPLOADS / session_id).mkdir()
    session = {"id": session_id, "name": name or "New campaign",
               "created_at": _now(), "updated_at": None, "generated_at": None, "form": {}}
    _save_session(session)
    _activate(session_id)
    return session


def current_session():
    """The open session with its form rows, or None in a fresh app."""
    return None if config.DATA is None else _load_session(config.DATA)


def save_draft(tables, name=None):
    """Remember the form as typed. Opens a new session when none is open yet."""
    session = current_session() or _new_session(name)
    session["form"] = _sanitize_form(tables)
    auto = campaign_name(session["form"])
    session["name"] = ((name or "").strip() or (auto and auto + session.get("name_suffix", "")) or session["name"])[:120]
    return _save_session(session)


def get_thresholds():
    return {"values": config.thresholds(), "defaults": config.DEFAULT_THRESHOLDS}


def set_thresholds(values):
    """Change the open session's rule guard thresholds. A missing or empty value goes back to its default."""
    session = current_session()
    if session is None:
        raise ValueError("open a campaign first")
    saved = {}
    for k, default in config.DEFAULT_THRESHOLDS.items():
        v = (values or {}).get(k)
        if v is None or str(v).strip() == "":
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{k} must be a number") from None
        if v < 0:
            raise ValueError(f"{k} cannot be negative")
        if v != default:
            saved[k] = int(v) if v.is_integer() else v
    if saved:
        session["thresholds"] = saved
    else:
        session.pop("thresholds", None)
    _save_session(session)
    return get_thresholds()


def list_sessions():
    if not config.UPLOADS.is_dir():
        return []
    out = [_summary(_load_session(f), details=True) for f in config.UPLOADS.iterdir()
           if f.is_dir() and config.DATASET_ID_RE.match(f.name)]
    return sorted(out, key=lambda s: s["updated_at"] or s["id"], reverse=True)


def open_session(session_id):
    _activate(session_id)                    # raises ValueError for an unknown id
    return current_session()


def create(tables, name=None):
    """Validate the form and write the dataset CSVs into the open session (a new one if none).

    Regenerating replaces the CSVs and drops every cached pipeline result, since scores were
    computed from the old rows. Downloaded or uploaded clips and the decision log are kept.
    """
    cleaned = clean(tables)                  # nothing is saved as a dataset unless it is valid
    session = save_draft(tables, name)
    folder = config.DATA
    old_videos = list(ingest.load_inputs()[0])
    backup = {n: (folder / n).read_bytes() for n in SCHEMA if (folder / n).exists()}
    try:
        for n, spec in SCHEMA.items():
            if cleaned[n] or n in REQUIRED_FILES:
                _write_csv(folder / n, [c["name"] for c in spec["cols"]], cleaned[n])
            else:
                (folder / n).unlink(missing_ok=True)
        ingest.load_inputs.cache_clear()
        new_videos = list(ingest.load_inputs()[0])   # the joins themselves can still reject the data
    except Exception as e:
        for n in SCHEMA:
            (folder / n).unlink(missing_ok=True)
        for n, data in backup.items():
            (folder / n).write_bytes(data)
        ingest.load_inputs.cache_clear()
        raise DatasetError([{"file": None, "row": None, "column": None, "message": str(e)}]) from e
    for vid in {*old_videos, *new_videos}:
        if _SAFE_ID.match(vid):
            pipeline.invalidate(vid)
    shutil.rmtree(config.RUNS / "recaps", ignore_errors=True)
    jobs.reset()
    session["generated_at"] = _now()
    _save_session(session)
    return describe()


def copy_session(session_id):
    """Duplicate a saved session: form, dataset CSVs, clips, pipeline results, reviews and ground truth.

    Uploaded videos are referenced by file:// URLs into the session's own folder; those are rewritten
    to point into the copy (clip_url.txt too, so the copied clips are not seen as stale and re-scored).
    The copy is saved alongside the original; the open session does not change.
    """
    if not config.DATASET_ID_RE.match(session_id or "") or not (config.UPLOADS / session_id).is_dir():
        raise ValueError(f"unknown session {session_id!r}")
    src = config.UPLOADS / session_id
    new_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dst = config.UPLOADS / new_id
    shutil.copytree(src, dst)
    old_uri, new_uri = src.resolve().as_uri(), dst.resolve().as_uri()
    for p in dst.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".csv", ".txt", ".jsonl") and p.stat().st_size < MAX_FILE_BYTES:
            text = p.read_text(encoding="utf-8", errors="strict")
            if old_uri in text:
                p.write_text(text.replace(old_uri, new_uri), encoding="utf-8")
    session = _load_session(dst)
    suffix = session.get("name_suffix", "") + " (copy)"      # kept when autosave renames it after its campaign
    session.update(id=new_id, name=((campaign_name(session.get("form")) or session.get("name") or "Campaign") + suffix)[:120],
                   name_suffix=suffix, created_at=_now(), copied_from=session_id)
    _save_session(session)
    return _summary(session)


def delete_session(session_id):
    """Delete a saved session's folder for good: form, CSVs, clips, results, reviews, ground truth.
    Deleting the open session closes it first, back to the clean state the app starts in."""
    folder = config.UPLOADS / str(session_id or "")
    if not config.DATASET_ID_RE.match(session_id or "") or not folder.is_dir() \
            or folder.resolve().parent != config.UPLOADS.resolve():
        raise ValueError(f"unknown session {session_id!r}")
    if config.DATA is not None and config.DATA.resolve() == folder.resolve():
        _activate(None)
    shutil.rmtree(folder)
    return list_sessions()


def reset():
    """Close the session (it stays saved) and go back to the clean state the app starts in."""
    _activate(None)
    return describe()


def _gt_state(session):
    return session.setdefault("ground_truth", {"rows": {}, "locked_at": None})


def _gt_cols():
    return [c for c in GROUND_TRUTH if not c["auto"]]


def _show(col, v):
    """One dataset value as display text for the video viewer."""
    v = ingest.val(v)
    if v is None or str(v).strip() == "":
        return "—"
    if col["kind"] == "int":
        return f"{int(float(v)):,}"
    if col["kind"] == "float":
        return f"{float(v):,.2f}".rstrip("0").rstrip(".")
    if col["kind"] == "json":
        try:
            return " · ".join(f"{k} {int(n):,}" for k, n in json.loads(v).items()) or "—"
        except (ValueError, TypeError, AttributeError):
            return str(v)
    return str(v)


def _video_info(row):
    """What the dataset says about a video and its creator, as labelled sections for the viewer."""
    skip = {"creator_id", "campaign_id", "caption", "video_url"}       # shown elsewhere in the viewer
    section = lambda f: [[c["label"], _show(c, row.get(c["name"]))] for c in SCHEMA[f]["cols"] if c["name"] not in skip]
    d = flatten.derived_metrics(row)
    pct = lambda x: "—" if x is None else f"{x * 100:.2f}%"
    return [["Creator", section("creators.csv")],
            ["Video", section("videos.csv")],
            ["Rates", [["Engagement rate", pct(d["engagement_rate"])], ["Save rate", pct(d["save_rate"])],
                       ["Share rate", pct(d["share_rate"])],
                       ["Views per reach", "—" if d["views_per_reach"] is None else f"{d['views_per_reach']:.2f}x"],
                       ["Views per hour", "—" if d["views_per_hour"] is None else f"{d['views_per_hour']:,.0f}"]]]]


def _viewer(vid, row):
    """What the video viewer needs: a link the browser can open (file:// URLs cannot), the clip, the info."""
    url = ingest.video_url(vid) or ""
    return {"url": url if url.startswith(("http://", "https://")) else None,
            "clip": ingest.clip_path(vid) is not None, "info": _video_info(row)}


def ground_truth():
    """The open campaign's ground-truth form: one row per video, the saved draft or locked values."""
    session = current_session()
    if session is None:
        raise ValueError("open a campaign first")
    gt = _gt_state(session)
    videos, _ = ingest.load_inputs()
    target = ingest.val(next(iter(videos.values()))["target_cac"]) if videos else None
    values = gt["rows"]
    locked_csv = config.DATA / "ground_truth.csv"
    if gt.get("locked_at") and locked_csv.exists():   # show exactly what was locked, derived values included
        values = {r["video_id"]: r for r in pd.read_csv(locked_csv, dtype=str, keep_default_na=False).to_dict("records")}
    rows = [{"video_id": vid, "creator": row.get("handle"), "caption": row.get("caption"),
             **_viewer(vid, row),
             **{c["name"]: (values.get(vid) or {}).get(c["name"], "") for c in _gt_cols()}}
            for vid, row in videos.items()]
    return {"cols": GROUND_TRUTH, "rows": rows, "target_cac": target,
            **{k: gt.get(k) for k in ("locked_at", "sha256", "file", "scored_at_lock", "videos_at_lock")}}


def _gt_input(rows):
    """{video_id: {column: text}} from the form, for this campaign's videos only."""
    videos = ingest.load_inputs()[0]
    out = {}
    for r in rows or []:
        vid = str((r or {}).get("video_id") or "")
        if vid in videos:
            out[vid] = {c["name"]: "" if r.get(c["name"]) is None else str(r.get(c["name"])) for c in _gt_cols()}
    return out


def save_ground_truth_draft(rows):
    session = current_session()
    if session is None:
        raise ValueError("open a campaign first")
    gt = _gt_state(session)
    if gt.get("locked_at"):
        raise GroundTruthLocked("the ground truth is locked and can no longer change")
    gt["rows"] = _gt_input(rows)
    _save_session(session)
    return ground_truth()


def lock_ground_truth(rows):
    """Validate, write ground_truth.csv (plus a dated copy) and lock it for good.

    Evaluation measures against this file only, so it must be fixed before results are looked
    at: the lock records its SHA-256 and how many videos were already scored at that moment.
    """
    session = current_session()
    if session is None:
        raise ValueError("open a campaign first")
    gt = _gt_state(session)
    if gt.get("locked_at"):
        raise GroundTruthLocked("the ground truth is already locked")
    gt["rows"] = _gt_input(rows)
    _save_session(session)                   # the draft is kept even when the lock is refused

    videos = ingest.load_inputs()[0]
    target = ingest.num((next(iter(videos.values()), {}) or {}).get("target_cac"))
    errors, out = [], []
    for i, vid in enumerate(videos):
        raw = gt["rows"].get(vid) or {}
        if not any(v.strip() for v in raw.values()):
            continue                         # a video without known results is left out
        r = {"video_id": vid}
        for c in _gt_cols():
            try:
                r[c["name"]] = _clean_cell(c, raw.get(c["name"]))
            except ValueError as e:
                r[c["name"]] = ""
                errors.append({"file": "ground_truth.csv", "row": i, "column": c["name"], "message": f"{vid}: {c['label']} {e}"})
        if not r["expected_verdict"]:
            errors.append({"file": "ground_truth.csv", "row": i, "column": "expected_verdict",
                           "message": f"{vid}: Expected verdict is required for a video with results"})
        if not r["cac"] and r["ad_spend"] and r["conversions"] and float(r["conversions"]) > 0:
            r["cac"] = _num_text(round(float(r["ad_spend"]) / float(r["conversions"]), 2))
        if not r["performed_well_after_scaling"] and r["cac"] and target is not None:
            r["performed_well_after_scaling"] = "yes" if float(r["cac"]) <= target else "no"
        out.append(r)
    if not out and not errors:
        errors.append({"file": "ground_truth.csv", "row": None, "column": None,
                       "message": "enter the real results of at least one video"})
    if errors:
        raise DatasetError(errors)

    stamp = datetime.now(timezone.utc)
    _write_csv(config.DATA / "ground_truth.csv", ANSWER_KEYS["ground_truth.csv"], out)
    dated = f"ground_truth.locked-{stamp:%Y%m%dT%H%M%SZ}.csv"
    shutil.copyfile(config.DATA / "ground_truth.csv", config.DATA / dated)
    scored = sum(pipeline.status(v)["scored"] for v in videos)
    gt.update(locked_at=stamp.isoformat(timespec="seconds"), file=dated, videos_at_lock=len(out),
              sha256=hashlib.sha256((config.DATA / "ground_truth.csv").read_bytes()).hexdigest(),
              scored_at_lock=scored)
    _save_session(session)
    return ground_truth()


def _mr_cols():
    return [c for c in MANUAL_REVIEW if not c["auto"]]


def _mr_sheet(session, reviewer):
    name = " ".join(str(reviewer or "").split())[:60]
    if not name:
        raise DatasetError([{"file": MANUAL_REVIEW_FILE, "row": None, "column": "reviewer",
                             "message": "type your name in the Reviewer field first"}])
    return session.setdefault("manual_review", {}).setdefault(name, {"rows": {}, "submitted_at": None}), name


def _open_session():
    session = current_session()
    if session is None:
        raise ValueError("open a campaign first")
    return session


def manual_review(reviewer):
    """One reviewer's blind sheet for the open campaign: per video the signals they may see, and their answers."""
    session = _open_session()
    sheet, name = _mr_sheet(session, reviewer)
    videos = ingest.load_inputs()[0]
    rows = [{"video_id": vid, **{f: v.get(f) for f in BLIND_FIELDS},
             **{c["name"]: (sheet["rows"].get(vid) or {}).get(c["name"], "") for c in _mr_cols()}}
            for vid, v in videos.items()]
    first = next(iter(videos.values()), {})
    return {"cols": MANUAL_REVIEW, "rows": rows, "reviewer": name,
            "campaign": {k: first.get(k) for k in ("brand", "brand_category", "product_type", "target_cac", "target_countries")},
            "reviewers": [{"reviewer": n, "submitted_at": r.get("submitted_at"), "rows": _filled(r["rows"].values())}
                          for n, r in session.get("manual_review", {}).items()],
            **{k: sheet.get(k) for k in ("submitted_at", "scored_at_submit")}}


def _mr_input(rows):
    videos = ingest.load_inputs()[0]
    return {str(r.get("video_id")): {c["name"]: "" if r.get(c["name"]) is None else str(r.get(c["name"])) for c in _mr_cols()}
            for r in rows or [] if str((r or {}).get("video_id") or "") in videos}


def save_manual_review_draft(reviewer, rows):
    session = _open_session()
    sheet, name = _mr_sheet(session, reviewer)
    if sheet.get("submitted_at"):
        raise ReviewSubmitted(f"{name}'s review is submitted and can no longer change")
    sheet["rows"] = _mr_input(rows)
    _save_session(session)
    return manual_review(name)


def submit_manual_review(reviewer, rows):
    """Validate one reviewer's sheet, freeze it and rewrite manual_review.csv from every submitted sheet.

    The submit records how many videos were already scored, as the ground-truth lock does: a blind
    baseline is only blind if it was made before the scores existed.
    """
    session = _open_session()
    sheet, name = _mr_sheet(session, reviewer)
    if sheet.get("submitted_at"):
        raise ReviewSubmitted(f"{name}'s review is already submitted")
    sheet["rows"] = _mr_input(rows)
    _save_session(session)                   # the draft is kept even when the submit is refused

    videos = ingest.load_inputs()[0]
    errors, n = [], 0
    for i, vid in enumerate(videos):
        raw = sheet["rows"].get(vid) or {}
        if not any(v.strip() for v in raw.values()):
            continue                         # a video the reviewer skipped is left out
        n += 1
        for c in _mr_cols():
            try:
                _clean_cell(c, raw.get(c["name"]))
            except ValueError as e:
                errors.append({"file": MANUAL_REVIEW_FILE, "row": i, "column": c["name"], "message": f"{vid}: {c['label']} {e}"})
        if not raw.get("decision", "").strip():
            errors.append({"file": MANUAL_REVIEW_FILE, "row": i, "column": "decision",
                           "message": f"{vid}: Decision is required for a reviewed video"})
    if not n and not errors:
        errors.append({"file": MANUAL_REVIEW_FILE, "row": None, "column": None, "message": "decide at least one video"})
    if errors:
        raise DatasetError(errors)

    sheet.update(submitted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 scored_at_submit=sum(pipeline.status(v)["scored"] for v in videos))
    _write_manual_review(session)
    _save_session(session)
    return manual_review(name)


def reviews():
    """Every reviewer of the open campaign and their decisions per video, for the Reviews overview.

    A draft's decisions stay hidden until it is submitted, so a reviewer still working cannot
    copy another's calls. Expected verdicts are shown only once the ground truth is locked.
    """
    session = _open_session()
    videos = ingest.load_inputs()[0]
    sheets = session.get("manual_review") or {}
    gt = _gt_state(session)
    truth = {}
    if gt.get("locked_at") and (config.DATA / "ground_truth.csv").exists():
        truth = {r["video_id"]: r for r in pd.read_csv(config.DATA / "ground_truth.csv", dtype=str,
                                                       keep_default_na=False).to_dict("records")}
    expected = {vid: {"SCALE": "SCALE", "": None}.get(r.get("expected_verdict", ""), "DO_NOT_SCALE")
                for vid, r in truth.items()}       # the reviewer's two choices: anything but SCALE is DO_NOT_SCALE

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    people = []
    for name, sheet in sheets.items():
        done = bool(sheet.get("submitted_at"))
        rows = {vid: r for vid, r in (sheet.get("rows") or {}).items() if vid in videos}
        decided = {vid: r for vid, r in rows.items() if str(r.get("decision") or "").strip()}
        mins = [m for m in (num(r.get("minutes_spent")) for r in decided.values()) if m is not None]
        judged = [vid for vid in decided if expected.get(vid)] if done else []
        people.append({
            "reviewer": name, "submitted_at": sheet.get("submitted_at"), "scored_at_submit": sheet.get("scored_at_submit"),
            "reviewed": len(decided), "videos": len(videos),
            "scale": sum(r["decision"].upper() == "SCALE" for r in decided.values()) if done else None,
            "total_minutes": round(sum(mins), 1) if mins and done else None,
            "mean_minutes": round(sum(mins) / len(mins), 1) if mins and done else None,
            "correct": sum(decided[v]["decision"].upper() == expected[v] for v in judged) if judged else None,
            "judged": len(judged)})

    out_videos = []
    for vid, row in videos.items():
        calls = {}
        for name, sheet in sheets.items():
            r = (sheet.get("rows") or {}).get(vid) or {}
            if sheet.get("submitted_at") and str(r.get("decision") or "").strip():
                calls[name] = {"decision": r["decision"].upper(), "reason": r.get("reason", ""),
                               "minutes": r.get("minutes_spent", "")}
        picks = {c["decision"] for c in calls.values()}
        out_videos.append({"video_id": vid, "creator": row.get("handle"), "caption": row.get("caption"),
                           **_viewer(vid, row), "calls": calls,
                           "agreement": None if len(calls) < 2 else ("agree" if len(picks) == 1 else "split"),
                           "expected": (truth.get(vid) or {}).get("expected_verdict") or None})
    both = [v for v in out_videos if v["agreement"]]
    return {"reviewers": people, "videos": out_videos, "ground_truth_locked": bool(truth),
            "agreement": {"agree": sum(v["agreement"] == "agree" for v in both), "n": len(both)}}


def delete_manual_review(reviewer):
    """Remove one reviewer's sheet, draft or submitted, and rewrite manual_review.csv without it."""
    session = _open_session()
    name = " ".join(str(reviewer or "").split())[:60]
    sheets = session.get("manual_review") or {}
    if name not in sheets:
        raise ValueError(f"no review by {name!r} in this campaign")
    was_submitted = bool(sheets.pop(name).get("submitted_at"))
    if was_submitted:
        if any(s.get("submitted_at") for s in sheets.values()):
            _write_manual_review(session)
        else:
            (config.DATA / MANUAL_REVIEW_FILE).unlink(missing_ok=True)
    _save_session(session)
    return reviews()


def _write_manual_review(session):
    videos = ingest.load_inputs()[0]
    out = [{"video_id": vid, "reviewer": name, **{c["name"]: _clean_cell(c, raw.get(c["name"])) for c in _mr_cols()}}
           for name, sheet in session.get("manual_review", {}).items() if sheet.get("submitted_at")
           for vid, raw in sheet["rows"].items() if vid in videos and raw.get("decision", "").strip()]
    _write_csv(config.DATA / MANUAL_REVIEW_FILE, [c["name"] for c in MANUAL_REVIEW], out)


def save_answer_keys(files):
    """files: [(filename, bytes)]. Adds ground_truth.csv / ground_truth_media.csv to the
    active dataset for the Evaluation tab. They are never read by the scoring steps."""
    if config.DATA is None:
        raise ValueError("create the dataset first")
    picked = {}
    for filename, data in files:
        name = PurePath(filename or "").name.lower()
        if name == "ground_truth.csv":
            raise ValueError("ground_truth.csv comes from the Ground truth form in the Evaluation tab, "
                             "where it is locked; it cannot be uploaded")
        if name not in ANSWER_KEYS:
            raise ValueError(f"{filename}: expected {' or '.join(ANSWER_KEYS)}")
        try:
            df = pd.read_csv(io.BytesIO(data), dtype=str)
        except Exception as e:
            raise ValueError(f"{filename}: not a readable CSV ({e})") from e
        missing = [c for c in ANSWER_KEYS[name] if c not in df.columns]
        if missing:
            raise ValueError(f"{filename} is missing column(s): {', '.join(missing)}")
        unknown = sorted(set(df["video_id"].dropna()) - set(ingest.load_inputs()[0]))
        if unknown:
            raise ValueError(f"{filename}: video_id not in this dataset: {', '.join(unknown[:5])}")
        picked[name] = data
    if not picked:
        raise ValueError(f"choose {' and/or '.join(ANSWER_KEYS)}")
    for name, data in picked.items():
        (config.DATA / name).write_bytes(data)
    return describe()


def _store_video(upload, dest):
    """Stream a binary file object to `dest`, checking it is a video ffmpeg can read."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name("upload_" + dest.name)
    try:
        size = 0
        with tmp.open("wb") as f:
            while chunk := upload.read(1 << 20):
                size += len(chunk)
                if size > MAX_CLIP_BYTES:
                    raise ValueError(f"video is larger than {MAX_CLIP_BYTES // 2**20} MB")
                f.write(chunk)
        duration, _ = media.probe(tmp)
        if not duration:
            raise ValueError("not a video ffmpeg can read")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def save_clip(video_id, upload):
    """Store an uploaded video file as the clip for one video. upload is a binary file object."""
    _store_video(upload, config.MEDIA / video_id / "clip.mp4")
    ingest.set_video_url(video_id, "")       # an uploaded file wins over any URL
    return config.MEDIA / video_id / "clip.mp4"


def save_video_file(upload, filename):
    """Keep an uploaded video in the session's videos/ folder and return its file:// URL,
    which goes in the form's Video URL column like any other link. Opens a session if none is."""
    folder = (current_session() and config.DATA) or config.UPLOADS / _new_session()["id"]
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", PurePath(filename or "video").stem).strip("._")[:60] or "video"
    name = f"{datetime.now():%Y%m%d-%H%M%S}-{stem}{PurePath(filename or '').suffix.lower() or '.mp4'}"
    return _store_video(upload, folder / "videos" / name).resolve().as_uri()


def describe():
    files = [] if config.DATA is None else [
        n for n in [*SCHEMA, *ANSWER_KEYS, MANUAL_REVIEW_FILE] if (config.DATA / n).exists()]
    session = current_session()
    return {"id": config.DATASET_ID, "files": files, "videos": len(ingest.load_inputs()[0]),
            "session": session and _summary(session)}
