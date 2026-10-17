"""Step 1 - ingest. Load the input CSVs and locate each video's media folder.

ground_truth*.csv is deliberately NOT read here. It is the hidden answer key and is only
opened inside evaluate.py.
"""

import json
import math
from functools import lru_cache

import pandas as pd

from . import config

# Ids are names, not numbers: an uploaded dataset may well use 1, 2, 3.
ID_DTYPES = {"video_id": str, "creator_id": str, "campaign_id": str}


def val(x):
    """Return a clean Python value, or None when the source cell was blank.

    A blank cell means "the Instagram API returned no value". It must never become 0.
    """
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    if isinstance(x, str):
        return x if x.strip() else None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return x.item() if hasattr(x, "item") else x


def num(x):
    """val() for numbers: None stays None, everything else becomes a float."""
    v = val(x)
    return None if v is None else float(v)


def _read(name):
    p = config.DATA / name
    return pd.read_csv(p, dtype=ID_DTYPES) if p.exists() else pd.DataFrame()


@lru_cache(maxsize=1)
def load_inputs():
    """Join videos to creator, campaign and media rows; index comments by video.

    Returns (videos_by_id: dict[str, dict], comments_by_video: dict[str, DataFrame]).
    """
    videos = _read("videos.csv")
    df = videos.merge(_read("creators.csv"), on="creator_id", how="left", validate="many_to_one")
    df = df.merge(_read("campaigns.csv"), on="campaign_id", how="left", validate="many_to_one")

    media = _read("media.csv")
    if not media.empty:
        media = media.rename(columns={"notes": "media_notes", "video_url": "media_video_url"})
        df = df.merge(media, on="video_id", how="left", validate="one_to_one")
        if "media_video_url" in df:          # video_url may live in either file
            df["video_url"] = (df["video_url"].fillna(df["media_video_url"])
                               if "video_url" in df else df["media_video_url"])

    comments = _read("comments.csv")
    comments_by_video = {}
    if not comments.empty:
        comments_by_video = {
            vid: g.sort_values("like_count", ascending=False).reset_index(drop=True)
            for vid, g in comments.groupby("video_id")
        }
    videos_by_id = {r["video_id"]: {k: val(v) for k, v in r.items()} for _, r in df.iterrows()}
    return videos_by_id, comments_by_video


def _url_overrides():
    p = config.RUNS / "video_urls.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def video_url(video_id):
    """Where to fetch this video from: the URL set in the UI, else the dataset's
    video_url column. None means "use the clip in the dataset's media folder"."""
    overrides = _url_overrides()
    if video_id in overrides:
        return overrides[video_id] or None
    return val(load_inputs()[0].get(video_id, {}).get("video_url"))


def set_video_url(video_id, url):
    """Remember the reviewer's URL for a video. A blank URL clears it."""
    overrides = _url_overrides()
    overrides[video_id] = (url or "").strip()
    (config.RUNS / "video_urls.json").write_text(
        json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")


def downloaded_clip(video_id):
    """Where a video fetched from its URL is stored (a pipeline artifact, not dataset input)."""
    return config.RUNS / video_id / "clip.mp4"


def mark_downloaded(video_id, url):
    downloaded_clip(video_id).with_name("clip_url.txt").write_text(url, encoding="utf-8")


def clip_path(video_id):
    """The video file for this video, or None when there is none.

    With a URL set, only a download of that exact URL counts: a changed URL means the old
    file is stale. Without one, the dataset's own media folder is used.
    """
    url = video_url(video_id)
    if url:
        p = downloaded_clip(video_id)
        src = p.with_name("clip_url.txt")
        ok = p.exists() and src.exists() and src.read_text(encoding="utf-8").strip() == url
        return p if ok else None

    row = load_inputs()[0].get(video_id, {})
    for rel in (row.get("media_path"), f"media/{video_id}/clip.mp4"):
        if not rel:
            continue
        p = (config.DATA / str(rel)).resolve()
        if p.is_relative_to(config.DATA) and p.is_file():      # media_path is uploaded text
            return p
    return None


def media_folder(video_id):
    return config.MEDIA / video_id


def fixture_keyframes(video_id):
    """keyframes.csv rows for one video - the fallback when there is no clip to extract from."""
    df = _read("keyframes.csv")
    if df.empty:
        return []
    return [
        {"t_s": int(r["t_s"]), "scene_description": val(r.get("scene_description")),
         "on_screen_text": val(r.get("on_screen_text"))}
        for _, r in df[df["video_id"] == video_id].sort_values("t_s").iterrows()
    ]


def fixture_transcript(video_id):
    """transcripts.csv rows for one video - the fallback when there is no audio to transcribe."""
    df = _read("transcripts.csv")
    if df.empty:
        return []
    return [
        {"start_s": float(r["start_s"]), "end_s": float(r["end_s"]), "text": str(r["text"])}
        for _, r in df[df["video_id"] == video_id].sort_values("start_s").iterrows()
    ]
