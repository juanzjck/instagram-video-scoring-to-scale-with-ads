"""Download Instagram videos and collect every metric we can get for each one.

Two sources, used together:

1. yt-dlp (any public post, no token): the video file, thumbnail, caption, post date,
   creator handle, duration, resolution, likes, comment count, views (when Instagram
   exposes them) and the first page of comments. Logging in through browser cookies
   (--cookies-from-browser) makes this far more reliable.

2. Instagram Graph API (optional, IG_ACCESS_TOKEN + IG_USER_ID in .env):
   - business_discovery: followers / media count of any public Business or Creator account.
   - insights: reach, views, shares, saved, total interactions, average watch time and
     ALL comments, but only for posts owned by the account the token belongs to.
   - follower_demographics by country: only for that same account.
   Instagram never exposes per-video viewer countries, shares or reach for posts you
   do not own, so those cells stay blank for other creators' videos.

Output (same headers as ../data-template so it can be uploaded to full-workflow-api):
    <out>/videos.csv, comments.csv, media.csv, creators.csv
    <out>/raw/<shortcode>.json        everything collected for the post
    <out>/videos/<shortcode>.mp4      the clip
    <out>/thumbnails/<shortcode>.jpg

Usage:
    python ig_collect.py urls.txt
    python ig_collect.py urls.csv --out data --cookies-from-browser chrome
"""

import argparse
import csv
import json
import os
import math
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yt_dlp
from dotenv import load_dotenv

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # fall back to ffmpeg on PATH
    FFMPEG = None

GRAPH = "https://graph.facebook.com/{version}"

VIDEOS_COLS = ["video_id", "creator_id", "campaign_id", "media_product_type", "posted_at",
               "collected_at", "hours_since_post", "caption", "reach", "views", "likes",
               "comments", "shares", "saved", "total_interactions", "avg_watch_time_ms",
               "skip_rate_pct", "video_url"]
COMMENTS_COLS = ["comment_id", "video_id", "text", "timestamp", "username", "like_count"]
MEDIA_COLS = ["video_id", "media_path", "thumbnail_path", "duration_s", "aspect_ratio",
              "resolution", "audio_type", "spoken_language", "has_burned_captions",
              "media_available", "notes"]
CREATORS_COLS = ["creator_id", "handle", "niche", "followers", "primary_language",
                 "follower_demographics_country"]

SHORTCODE_RE = re.compile(r"instagram\.com/(?:[^/]+/)?(?:p|reels?|tv)/([A-Za-z0-9_-]+)")


# ---------------------------------------------------------------- input

def read_inputs(path):
    """Return [{url, campaign_id}] from a .txt (one URL per line) or a .csv with a url column."""
    p = Path(path)
    rows = []
    if p.suffix.lower() == ".csv":
        with p.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                url = (r.get("url") or r.get("video_url") or "").strip()
                if url:
                    rows.append({"url": url, "campaign_id": (r.get("campaign_id") or "").strip()})
    else:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                rows.append({"url": line, "campaign_id": ""})
    return rows


def shortcode_of(url):
    m = SHORTCODE_RE.search(url)
    return m.group(1) if m else None


# ---------------------------------------------------------------- yt-dlp

def ytdlp_collect(url, out, cookies_browser=None, cookies_file=None):
    opts = {
        "outtmpl": {"default": str(out / "videos" / "%(id)s.%(ext)s"),
                    "thumbnail": str(out / "thumbnails" / "%(id)s.%(ext)s")},
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "getcomments": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": False,  # carousels come back as playlists; we keep every video in them
    }
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
    if cookies_browser:
        # "chrome" or "chrome:Profile 1" (profile folder from chrome://version)
        browser, _, profile = cookies_browser.partition(":")
        opts["cookiesfrombrowser"] = (browser, profile or None)
    if cookies_file:
        opts["cookiefile"] = cookies_file
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.sanitize_info(info)


def find_file(folder, stem, exts):
    for ext in exts:
        f = folder / f"{stem}.{ext}"
        if f.exists():
            return f
    return None


def probe_duration(path):
    """Seconds, read from ffmpeg's header dump (yt-dlp often has no duration without login)."""
    try:
        err = subprocess.run([FFMPEG or "ffmpeg", "-i", str(path)], capture_output=True, text=True).stderr
        h, m, sec = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err).groups()
        return round(int(h) * 3600 + int(m) * 60 + float(sec), 2)
    except Exception:
        return None


# ---------------------------------------------------------------- Graph API

class Graph:
    def __init__(self, token, ig_user_id, version):
        self.token, self.ig_user_id = token, ig_user_id
        self.base = GRAPH.format(version=version)
        self._own_media = None

    def get(self, path, **params):
        params["access_token"] = self.token
        r = requests.get(f"{self.base}/{path}", params=params, timeout=30)
        data = r.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message", str(data["error"])))
        return data

    def paged(self, path, **params):
        data = self.get(path, **params)
        while True:
            yield from data.get("data", [])
            nxt = data.get("paging", {}).get("next")
            if not nxt:
                return
            data = requests.get(nxt, timeout=30).json()

    def own_media(self):
        """shortcode -> media id for every post of the token's account."""
        if self._own_media is None:
            self._own_media = {}
            for m in self.paged(f"{self.ig_user_id}/media", fields="id,shortcode", limit=100):
                self._own_media[m["shortcode"]] = m["id"]
        return self._own_media

    def creator(self, username):
        fields = ("business_discovery.username(%s)"
                  "{username,name,biography,followers_count,follows_count,media_count,website}" % username)
        return self.get(self.ig_user_id, fields=fields).get("business_discovery", {})

    def public_media(self, username, shortcode):
        """Public counts for a post of any Business/Creator account (scans its latest 200 posts)."""
        fields = ("business_discovery.username(%s){media.limit(200)"
                  "{id,shortcode,like_count,comments_count,view_count,media_product_type,timestamp}}" % username)
        try:
            media = self.get(self.ig_user_id, fields=fields)["business_discovery"]["media"]["data"]
        except (RuntimeError, KeyError):
            # view_count is not available on every API version; retry without it
            fields = fields.replace("view_count,", "")
            media = self.get(self.ig_user_id, fields=fields).get("business_discovery", {}) \
                .get("media", {}).get("data", [])
        return next((m for m in media if m.get("shortcode") == shortcode), None)

    def owned_details(self, media_id):
        media = self.get(media_id, fields="id,shortcode,media_type,media_product_type,caption,"
                                          "timestamp,permalink,like_count,comments_count,username")
        insights = {}
        for metric in ["reach", "views", "likes", "comments", "shares", "saved",
                       "total_interactions", "ig_reels_avg_watch_time",
                       "ig_reels_video_view_total_time", "follows", "profile_visits"]:
            # ask one by one: an unsupported metric for this media type fails the whole call
            try:
                d = self.get(f"{media_id}/insights", metric=metric)
                insights[metric] = d["data"][0]["values"][0]["value"]
            except Exception:
                pass
        comments = []
        for c in self.paged(f"{media_id}/comments", fields="id,text,timestamp,username,like_count,"
                                                          "replies{id,text,timestamp,username,like_count}",
                            limit=50):
            comments.append(c)
            comments.extend(c.get("replies", {}).get("data", []))
        return media, insights, comments

    def follower_countries(self):
        d = self.get(f"{self.ig_user_id}/insights", metric="follower_demographics", period="lifetime",
                     metric_type="total_value", breakdown="country")
        results = d["data"][0]["total_value"]["breakdowns"][0]["results"]
        return {r["dimension_values"][0]: r["value"] for r in results}

    def own_username(self):
        return self.get(self.ig_user_id, fields="username")["username"]


# ---------------------------------------------------------------- per-post assembly

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else ""


def blank(v):
    return "" if v is None else v


def collect_one(item, out, args, graph):
    url, campaign_id = item["url"], item["campaign_id"]
    record = {"input_url": url, "campaign_id": campaign_id,
              "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "errors": []}

    info = None
    try:
        info = ytdlp_collect(url, out, args.cookies_from_browser, args.cookies)
    except Exception as e:
        record["errors"].append(f"yt-dlp: {e}")

    # a carousel post comes back as a playlist: one entry per video
    entries = (info.get("entries") or [info]) if info else [{}]
    rows = []
    for idx, e in enumerate(entries):
        shortcode = shortcode_of(url) or e.get("id")
        video_id = e.get("id") or shortcode
        if len(entries) > 1:
            video_id = f"{shortcode}_{idx + 1}"
        handle = e.get("channel") or e.get("uploader_id") or (info or {}).get("channel") or ""
        rows.append({"video_id": video_id, "shortcode": shortcode, "handle": handle, "yt": e})
    record["ytdlp"] = info

    results = []
    for r in rows:
        e, handle, shortcode = r["yt"], r["handle"], r["shortcode"]
        api_media, insights, api_comments, public = {}, {}, [], None
        if graph and shortcode:
            try:
                if shortcode in graph.own_media():
                    api_media, insights, api_comments = graph.owned_details(graph.own_media()[shortcode])
                    handle = handle or api_media.get("username", "")
                elif handle:
                    public = graph.public_media(handle, shortcode)
            except Exception as ex:
                record["errors"].append(f"graph api: {ex}")
        record.setdefault("graph", []).append({"media": api_media, "insights": insights, "public": public})

        ts = e.get("timestamp")
        if not ts and api_media.get("timestamp"):
            ts = datetime.strptime(api_media["timestamp"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
        if not ts and public and public.get("timestamp"):
            ts = datetime.strptime(public["timestamp"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
        hours = round((time.time() - ts) / 3600, 1) if ts else ""

        likes = insights.get("likes", api_media.get("like_count", e.get("like_count")))
        if likes is None and public:
            likes = public.get("like_count")
        n_comments = insights.get("comments", api_media.get("comments_count", e.get("comment_count")))
        if n_comments is None and public:
            n_comments = public.get("comments_count")
        views = insights.get("views", e.get("view_count"))
        if views is None and public:
            views = public.get("view_count")
        watch = insights.get("ig_reels_avg_watch_time")
        product = api_media.get("media_product_type") or (public or {}).get("media_product_type") \
            or ("REELS" if "/reel" in url else "FEED")

        video_row = {
            "video_id": r["video_id"],
            "creator_id": f"cr_{handle}" if handle else "",
            "campaign_id": campaign_id,
            "media_product_type": product,
            "posted_at": iso(ts),
            "collected_at": record["collected_at"],
            "hours_since_post": hours,
            "caption": api_media.get("caption") or e.get("description") or "",
            "reach": blank(insights.get("reach")),
            "views": blank(views),
            "likes": blank(likes),
            "comments": blank(n_comments),
            "shares": blank(insights.get("shares")),
            "saved": blank(insights.get("saved")),
            "total_interactions": blank(insights.get("total_interactions")),
            "avg_watch_time_ms": blank(watch),
            "skip_rate_pct": "",  # shown in the Instagram app only, not in any API
            "video_url": url,
        }

        # API comments are complete (own posts); yt-dlp gives only the first page
        if api_comments:
            comments = [{"comment_id": c.get("id"), "text": c.get("text"), "timestamp": c.get("timestamp"),
                         "username": c.get("username"), "like_count": blank(c.get("like_count"))}
                        for c in api_comments]
        else:
            comments = [{"comment_id": c.get("id"), "text": c.get("text"), "timestamp": iso(c.get("timestamp")),
                         "username": c.get("author"), "like_count": blank(c.get("like_count"))}
                        for c in (e.get("comments") or [])]
        for c in comments:
            c["video_id"] = r["video_id"]

        vid_file = find_file(out / "videos", e.get("id", ""), ["mp4", "mkv", "webm", "mov"]) if e else None
        thumb = find_file(out / "thumbnails", e.get("id", ""), ["jpg", "jpeg", "webp", "png"]) if e else None
        w, h = e.get("width"), e.get("height")
        g = math.gcd(w, h) if w and h else 1
        duration = e.get("duration") or (probe_duration(vid_file) if vid_file else None)
        media_row = {
            "video_id": r["video_id"],
            "media_path": str(vid_file.relative_to(out)) if vid_file else "",
            "thumbnail_path": str(thumb.relative_to(out)) if thumb else "",
            "duration_s": blank(duration),
            "aspect_ratio": f"{w // g}:{h // g}" if w and h else "",
            "resolution": f"{w}x{h}" if w and h else "",
            "audio_type": "", "spoken_language": "", "has_burned_captions": "",
            "media_available": "yes" if vid_file else "no",
            "notes": "; ".join(record["errors"]),
        }
        results.append((video_row, comments, media_row, handle))

    raw_name = shortcode_of(url) or (rows[0]["video_id"] or "unknown")
    (out / "raw" / f"{raw_name}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str))
    return results, record["errors"]


# ---------------------------------------------------------------- main

def write_csv(path, cols, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help=".txt with one URL per line, or .csv with url[,campaign_id] columns")
    ap.add_argument("--out", default="output", help="output folder (default: output)")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER",
                    default=os.getenv("IG_COOKIES_BROWSER") or None,
                    help="log in with this browser's Instagram session: chrome, safari, firefox, edge…")
    ap.add_argument("--cookies", metavar="FILE", default=os.getenv("IG_COOKIES_FILE") or None,
                    help="Netscape cookies.txt file (alternative to --cookies-from-browser)")
    ap.add_argument("--delay", type=float, default=float(os.getenv("IG_DELAY_S", "5")),
                    help="seconds to wait between posts so Instagram does not rate-limit you (default 5)")
    args = ap.parse_args()

    out = Path(args.out)
    for sub in ("videos", "thumbnails", "raw"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    graph, own_handle, own_countries = None, None, None
    if os.getenv("IG_ACCESS_TOKEN") and os.getenv("IG_USER_ID"):
        graph = Graph(os.getenv("IG_ACCESS_TOKEN"), os.getenv("IG_USER_ID"),
                      os.getenv("IG_GRAPH_VERSION", "v23.0"))
        try:
            own_handle = graph.own_username()
            own_countries = graph.follower_countries()
        except Exception as ex:
            print(f"[graph] account info unavailable: {ex}", file=sys.stderr)
    else:
        print("[info] no IG_ACCESS_TOKEN/IG_USER_ID: reach, shares, saved, watch time and countries "
              "will be blank (public data only)", file=sys.stderr)

    items = read_inputs(args.input)
    videos, comments, media, handles = [], [], [], {}
    for i, item in enumerate(items, 1):
        print(f"[{i}/{len(items)}] {item['url']}")
        results, errors = collect_one(item, out, args, graph)
        for err in errors:
            print(f"    ! {err}", file=sys.stderr)
        for v, c, m, handle in results:
            videos.append(v)
            comments.extend(c)
            media.append(m)
            if handle:
                handles.setdefault(handle, None)
            print(f"    {v['video_id']}: views={v['views']} likes={v['likes']} comments={v['comments']} "
                  f"({len(c)} downloaded) shares={v['shares'] or '-'} file={m['media_path'] or '-'}")
        if i < len(items):
            time.sleep(args.delay)

    creators = []
    for handle in handles:
        row = {"creator_id": f"cr_{handle}", "handle": handle, "niche": "", "followers": "",
               "primary_language": "", "follower_demographics_country": ""}
        if graph:
            try:
                row["followers"] = blank(graph.creator(handle).get("followers_count"))
            except Exception as ex:
                print(f"[graph] {handle}: {ex}", file=sys.stderr)
            if handle == own_handle and own_countries:
                row["follower_demographics_country"] = json.dumps(own_countries)
        creators.append(row)

    write_csv(out / "videos.csv", VIDEOS_COLS, videos)
    write_csv(out / "comments.csv", COMMENTS_COLS, comments)
    write_csv(out / "media.csv", MEDIA_COLS, media)
    write_csv(out / "creators.csv", CREATORS_COLS, creators)
    print(f"\nDone: {len(videos)} videos, {len(comments)} comments, {len(creators)} creators -> {out}/")


if __name__ == "__main__":
    main()
