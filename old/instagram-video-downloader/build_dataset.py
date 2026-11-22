"""Build videos.csv, comments.csv and creators.csv from what ig_collect.py already fetched,
collect whatever real data is still missing, and only then fill the rest.

Steps:

1. raw        read <raw>/<shortcode>.json written by ig_collect.py (caption, likes, comments…)
2. collect    ask Instagram again for the cells that are still blank, with your browser login
              (and the Graph API when IG_ACCESS_TOKEN / IG_USER_ID are set):
                - creator profile: followers, category, bio and the latest ~12 posts with their
                  likes, comments and views (used to learn each creator's views-per-like)
                - each post: likes, comments, views / plays
              Every answer is cached in <raw>/../profiles/ and <raw>/../posts/, so it is only
              requested once. On a rate limit (HTTP 429) the script stops asking and goes on.
              Cache files can also be written by hand (e.g. numbers read in the browser from the
              profile page and its Reels grid); a cached file always wins over a request.
3. forecast   followers, views, likes and reach that are still blank are estimated from the
              real numbers we have, in this order of preference:
                - the creator's own posts (e.g. views = likes x their median views/likes)
                - the other creators/videos in this dataset
                - a typical Instagram ratio, when there is no real data to learn from
4. dummy      plausible made-up numbers for what Instagram never shows for other people's
              posts: shares, saved, watch time, skip rate, follower countries. Seeded by the
              id, so every run gives the same values.

<out>/data_sources.csv lists the source (instagram, collected, forecast, derived, dummy) and the
method of every cell this script filled. The template files are only read for their headers.

Usage:
    python build_dataset.py
    python build_dataset.py --campaign-id cmp_xtb --cookies-from-browser chrome --delay 10
    python build_dataset.py --no-fetch          # skip step 2, use the cache only
"""

import argparse
import csv
import http.cookiejar
import json
import os
import random
import re
import statistics
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from yt_dlp.cookies import extract_cookies_from_browser

from ig_collect import Graph, blank, iso, shortcode_of

WEB_API = "https://www.instagram.com/api/v1"
IG_APP_ID = "936619743392459"  # the id instagram.com's own web client sends
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

# typical Instagram ratios, used only when there is no real data to learn from
PRIOR_LIKES_PER_VIEW = 0.035     # ~3.5 % of viewers like a reel
PRIOR_LIKES_PER_FOLLOWER = 0.02  # ~2 % of followers like a post
PRIOR_REACH_PER_VIEW = 0.70      # unique accounts / plays (people replay reels)
MIN_SAMPLES = 3                  # real pairs needed before a ratio is trusted

NICHE_WORDS = {
    "finance": ["invert", "inversi", "invest", "bolsa", "trading", "broker", "bróker", "acciones",
                "dinero", "ahorr", "interés compuesto", "finanz", "stock", "money"],
    "beauty": ["piel", "maquillaje", "skincare", "makeup", "belleza", "beauty"],
    "fitness": ["gym", "entren", "rutina", "workout", "fitness", "proteína"],
    "food": ["receta", "cocina", "recipe", "comida", "food"],
    "tech": ["app", "tecnolog", "iphone", "android", "software", "tech"],
}
# Instagram account categories that say nothing about the niche
GENERIC_CATEGORIES = ["creator", "blog", "service", "personal", "public figure", "artist"]
LANG_WORDS = {
    "es": ["de", "que", "el", "la", "y", "en", "para", "tu", "con", "es", "los", "por"],
    "en": ["the", "and", "to", "of", "you", "your", "is", "for", "with", "this", "in"],
}
# where Spanish- / English-speaking followers usually are
COUNTRIES = {"es": ["MX", "CO", "ES", "PE", "AR", "EC", "CL", "US", "VE"],
             "en": ["US", "GB", "CA", "AU", "IN", "PH", "NG"]}


# ---------------------------------------------------------------- csv

def read_header(path):
    with path.open(newline="", encoding="utf-8") as f:
        return next(csv.reader(f))


def read_rows(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, cols, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def parse_ts(value):
    """Unix seconds from an ISO string ('...Z' or '+0000'), or None."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def num(v):
    return None if v in ("", None) else float(v)


# ---------------------------------------------------------------- 1. raw -> rows

def rows_from_record(record, campaign_id):
    """(video_rows, comment_rows, handles, durations) for one raw/<shortcode>.json."""
    url = record.get("input_url", "")
    info = record.get("ytdlp") or {}
    entries = info.get("entries") or [info]
    graphs = record.get("graph") or []
    collected_at = record.get("collected_at", "")
    collected_ts = parse_ts(collected_at)

    videos, comments, handles, durations = [], [], [], {}
    for idx, e in enumerate(entries):
        shortcode = shortcode_of(url) or e.get("id")
        video_id = f"{shortcode}_{idx + 1}" if len(entries) > 1 else (e.get("id") or shortcode)
        g = graphs[idx] if idx < len(graphs) else {}
        api_media, insights, public = g.get("media") or {}, g.get("insights") or {}, g.get("public") or {}

        handle = e.get("channel") or e.get("uploader_id") or info.get("channel") or api_media.get("username") or ""
        if handle:
            handles.append(handle)
        durations[video_id] = e.get("duration")

        ts = e.get("timestamp") or parse_ts(api_media.get("timestamp")) or parse_ts(public.get("timestamp"))
        # age at collection time, so re-running this script later gives the same numbers
        hours = round((collected_ts - ts) / 3600, 1) if ts and collected_ts else ""

        likes = insights.get("likes", api_media.get("like_count", e.get("like_count")))
        if likes is None:
            likes = public.get("like_count")
        n_comments = insights.get("comments", api_media.get("comments_count", e.get("comment_count")))
        if n_comments is None:
            n_comments = public.get("comments_count")
        views = insights.get("views", e.get("view_count"))
        if views is None:
            views = public.get("view_count")
        product = api_media.get("media_product_type") or public.get("media_product_type") \
            or ("REELS" if "/reel" in url else "FEED")

        videos.append({
            "video_id": video_id,
            "shortcode": shortcode,
            "creator_id": f"cr_{handle}" if handle else "",
            "campaign_id": campaign_id or record.get("campaign_id", ""),
            "media_product_type": product,
            "posted_at": iso(ts),
            "collected_at": collected_at,
            "hours_since_post": hours,
            "caption": api_media.get("caption") or e.get("description") or "",
            "reach": blank(insights.get("reach")),
            "views": blank(views),
            "likes": blank(likes),
            "comments": blank(n_comments),
            "shares": blank(insights.get("shares")),
            "saved": blank(insights.get("saved")),
            "total_interactions": blank(insights.get("total_interactions")),
            "avg_watch_time_ms": blank(insights.get("ig_reels_avg_watch_time")),
            "skip_rate_pct": "",  # shown in the Instagram app only, not in any API
            "video_url": url,
        })

        for c in e.get("comments") or []:
            comments.append({"comment_id": c.get("id"), "video_id": video_id, "text": c.get("text"),
                             "timestamp": iso(c.get("timestamp")), "username": c.get("author"),
                             "like_count": blank(c.get("like_count"))})
    return videos, comments, handles, durations


# ---------------------------------------------------------------- 2. collect real data

def media_id(shortcode):
    """Numeric media id hidden in a post shortcode (base64 with Instagram's alphabet)."""
    n = 0
    for ch in shortcode[:11]:  # longer codes carry a suffix that is not part of the id
        n = n * 64 + SHORTCODE_ALPHABET.index(ch)
    return str(n)


def ig_session(cookies_browser=None, cookies_file=None):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "x-ig-app-id": IG_APP_ID, "X-Requested-With": "XMLHttpRequest"})
    try:
        if cookies_browser:
            browser, _, profile = cookies_browser.partition(":")
            jar = extract_cookies_from_browser(browser, profile or None)
        elif cookies_file:
            jar = http.cookiejar.MozillaCookieJar(cookies_file)
            jar.load(ignore_discard=True, ignore_expires=True)
        else:
            jar = []
        for c in jar:
            if "instagram.com" in c.domain:
                s.cookies.set(c.name, c.value, domain=c.domain)
    except Exception as ex:
        print(f"[collect] could not read cookies: {ex}")
    s.headers["X-CSRFToken"] = s.cookies.get("csrftoken", "")
    return s


class Collector:
    """Real numbers from Instagram, cached on disk. Stops asking after a rate limit."""

    def __init__(self, cache_dir, session=None, graph=None, delay=5.0):
        self.profiles, self.posts = cache_dir / "profiles", cache_dir / "posts"
        self.session, self.graph, self.delay = session, graph, delay
        self.blocked = session is None and graph is None
        self._last = 0.0

    def _cached(self, path, fetch):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if self.blocked:
            return None
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        data = fetch()
        if data:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return data

    def _web(self, path, label, **params):
        if not self.session:
            return None
        status = "no response"
        try:
            r = self.session.get(f"{WEB_API}/{path}", params=params, timeout=20)
            status = f"HTTP {r.status_code}"
            if r.status_code == 429:
                self.blocked = True
                print(f"[collect] rate-limited by Instagram ({label}); using cache and forecasts for the rest")
                return None
            return r.json()
        except Exception:
            print(f"[collect] {label}: not available ({status})")
            return None

    def profile(self, handle):
        """{followers, category, biography, recent: [{shortcode, likes, comments, views}]} or None."""
        def fetch():
            out = None
            if self.graph:
                try:
                    d = self.graph.creator(handle)
                    out = {"followers": d.get("followers_count"), "category": None,
                           "biography": d.get("biography"), "recent": [], "source": "graph_api"}
                except Exception as ex:
                    print(f"[collect] graph api {handle}: {ex}")
            d = self._web("users/web_profile_info/", f"profile {handle}", username=handle)
            user = ((d or {}).get("data") or {}).get("user")
            if user:
                recent = []
                for edge in (user.get("edge_owner_to_timeline_media") or {}).get("edges", []):
                    n = edge.get("node") or {}
                    recent.append({
                        "shortcode": n.get("shortcode"),
                        "is_video": n.get("is_video"),
                        "likes": (n.get("edge_liked_by") or n.get("edge_media_preview_like") or {}).get("count"),
                        "comments": (n.get("edge_media_to_comment") or {}).get("count"),
                        "views": n.get("video_view_count"),
                        "taken_at": n.get("taken_at_timestamp"),
                    })
                out = {"followers": (user.get("edge_followed_by") or {}).get("count"),
                       "category": user.get("category_name") or user.get("business_category_name"),
                       "biography": user.get("biography"), "recent": recent, "source": "web_profile"}
            return out
        return self._cached(self.profiles / f"{handle}.json", fetch)

    def post(self, shortcode, handle):
        """{likes, comments, views} for one post, or None."""
        def fetch():
            if self.graph and handle:
                try:
                    m = self.graph.public_media(handle, shortcode)
                    if m:
                        return {"likes": m.get("like_count"), "comments": m.get("comments_count"),
                                "views": m.get("view_count"), "source": "graph_api"}
                except Exception as ex:
                    print(f"[collect] graph api {shortcode}: {ex}")
            d = self._web(f"media/{media_id(shortcode)}/info/", f"post {shortcode}")
            items = (d or {}).get("items") or []
            if not items:
                return None
            it = items[0]
            return {"likes": it.get("like_count"), "comments": it.get("comment_count"),
                    "views": it.get("play_count") or it.get("ig_play_count") or it.get("view_count"),
                    "source": "web_media_info"}
        return self._cached(self.posts / f"{shortcode}.json", fetch)


# ---------------------------------------------------------------- derived values

def guess_niche(text):
    text = text.lower()
    hits = {n: sum(text.count(w) for w in words) for n, words in NICHE_WORDS.items()}
    best = max(hits, key=hits.get)
    return best if hits[best] else ""


def guess_language(text):
    words = re.findall(r"[a-záéíóúñü]+", text.lower())
    hits = {lang: sum(words.count(w) for w in stop) for lang, stop in LANG_WORDS.items()}
    best = max(hits, key=hits.get)
    return best if hits[best] else ""


# ---------------------------------------------------------------- 3. forecast

def ratio(pairs):
    """Median of a/b over pairs with both values, or None with too few samples."""
    vals = [a / b for a, b in pairs if a and b]
    return statistics.median(vals) if len(vals) >= MIN_SAMPLES else None


def pick_ratio(own, everyone, prior, what):
    """(ratio, method text): the creator's own data, else the dataset, else the prior."""
    r = ratio(own)
    if r is not None:
        return r, f"{what} from the creator's {len(own)} posts with real data"
    r = ratio(everyone)
    if r is not None:
        return r, f"{what} from {len(everyone)} posts with real data in this dataset"
    return prior, f"{what} from a typical Instagram ratio (no real data to learn from)"


# ---------------------------------------------------------------- 4. dummy values

def dummy_video(v, duration, rng):
    """Made-up Insights numbers for what the API never shows, scaled from the real engagement."""
    base = max(int(num(v["likes"]) or 0) + int(num(v["comments"]) or 0), 5)
    seconds = float(duration) if duration else 60.0
    return {
        "shares": int(base * rng.uniform(0.05, 0.25)),
        "saved": int(base * rng.uniform(0.10, 0.40)),  # how-to clips get saved a lot
        "avg_watch_time_ms": int(seconds * rng.uniform(0.15, 0.45) * 1000),
        "skip_rate_pct": round(rng.uniform(15, 55), 1),
    }


def dummy_countries(followers, language, rng):
    """Top-5 follower countries covering 75–95 % of the audience."""
    countries = rng.sample(COUNTRIES.get(language) or COUNTRIES["es"], 5)
    weights = sorted((rng.uniform(0.15, 1) ** 2 for _ in countries), reverse=True)
    share = rng.uniform(0.75, 0.95) * followers
    return {c: int(share * w / sum(weights)) for c, w in zip(countries, weights)}


# ---------------------------------------------------------------- main

def main():
    load_dotenv()
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default=str(here / "output" / "raw"), help="folder with <shortcode>.json files")
    ap.add_argument("--template", default=str(here.parent / "data-template"),
                    help="folder whose CSV headers are copied (read only)")
    ap.add_argument("--out", default=str(here / "dataset"), help="where the CSVs are written (default: dataset)")
    ap.add_argument("--campaign-id", default="", help="campaign_id for every video (default: the one in raw)")
    ap.add_argument("--niche", default="", help="niche for every creator (default: guessed from captions)")
    ap.add_argument("--language", default="", help="primary_language for every creator (default: guessed)")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER",
                    default=os.getenv("IG_COOKIES_BROWSER") or None,
                    help="log in with this browser's Instagram session to read profiles and posts")
    ap.add_argument("--cookies", metavar="FILE", default=os.getenv("IG_COOKIES_FILE") or None,
                    help="Netscape cookies.txt file (alternative to --cookies-from-browser)")
    ap.add_argument("--delay", type=float, default=float(os.getenv("IG_DELAY_S", "5")),
                    help="seconds between Instagram requests (default 5)")
    ap.add_argument("--no-fetch", action="store_true", help="do not ask Instagram, use the cache only")
    args = ap.parse_args()

    raw, template, out = Path(args.raw), Path(args.template), Path(args.out)
    if out.resolve() == template.resolve():
        ap.error("--out must not be the template folder")
    out.mkdir(parents=True, exist_ok=True)

    sources = []

    def mark(file, row_id, column, source, method=""):
        sources.append({"file": file, "id": row_id, "column": column, "source": source, "method": method})

    # ---- 1. raw
    videos, comments, handles, durations = [], [], {}, {}
    for f in sorted(raw.glob("*.json")):
        v, c, h, d = rows_from_record(json.loads(f.read_text(encoding="utf-8")), args.campaign_id)
        videos.extend(v)
        comments.extend(c)
        durations.update(d)
        for handle in h:
            handles.setdefault(handle, None)
    videos.sort(key=lambda r: r["posted_at"])
    # yt-dlp often has no duration without login; ig_collect.py probed the file instead
    for m in read_rows(raw.parent / "media.csv"):
        if not durations.get(m["video_id"]) and m.get("duration_s"):
            durations[m["video_id"]] = m["duration_s"]
    known = {r["handle"]: r for r in read_rows(raw.parent / "creators.csv")}
    creators = {}
    for handle in handles:
        prev = known.get(handle, {})
        creators[f"cr_{handle}"] = {
            "creator_id": f"cr_{handle}", "handle": handle,
            "niche": prev.get("niche", ""), "followers": prev.get("followers", ""),
            "primary_language": prev.get("primary_language", ""),
            "follower_demographics_country": prev.get("follower_demographics_country", "")}

    # ---- 2. collect real data for what is still blank
    session = graph = None
    if not args.no_fetch:
        session = ig_session(args.cookies_from_browser, args.cookies)
        if os.getenv("IG_ACCESS_TOKEN") and os.getenv("IG_USER_ID"):
            graph = Graph(os.getenv("IG_ACCESS_TOKEN"), os.getenv("IG_USER_ID"),
                          os.getenv("IG_GRAPH_VERSION", "v23.0"))
    collector = Collector(raw.parent, session, graph, args.delay)

    profiles = {}
    for cid, row in creators.items():
        profiles[cid] = collector.profile(row["handle"]) or {}
        if row["followers"] == "" and profiles[cid].get("followers") is not None:
            row["followers"] = profiles[cid]["followers"]
            mark("creators.csv", cid, "followers", "collected", profiles[cid].get("source", ""))

    for v in videos:
        if all(v[c] != "" for c in ("likes", "comments", "views")):
            continue
        handle = creators.get(v["creator_id"], {}).get("handle", "")
        recent = {p["shortcode"]: p for p in profiles.get(v["creator_id"], {}).get("recent", [])}
        found = recent.get(v["shortcode"])
        method = "creator's profile feed"
        if not found or any(found.get(k) is None for k in ("likes", "comments", "views")):
            found = collector.post(v["shortcode"], handle) or found
            method = (found or {}).get("source", method)
        for col in ("likes", "comments", "views"):
            if v[col] == "" and found and found.get(col) is not None:
                v[col] = found[col]
                mark("videos.csv", v["video_id"], col, "collected", method)

    # ---- 3. forecast followers, views, likes, reach from the real numbers
    def creator_posts(cid):
        """(likes, views) of every real post we know for a creator, once per shortcode."""
        posts = {p["shortcode"]: (p.get("likes"), p.get("views"))
                 for p in profiles.get(cid, {}).get("recent", []) if p.get("is_video") is not False}
        for v in videos:
            if v["creator_id"] == cid:
                posts[v["shortcode"]] = (num(v["likes"]), num(v["views"]))
        return list(posts.values())

    all_posts = {cid: creator_posts(cid) for cid in creators}
    everyone = [p for posts in all_posts.values() for p in posts]
    likes_follower_all = [(likes, num(creators[cid]["followers"]))
                          for cid, posts in all_posts.items() for likes, _ in posts
                          if creators[cid]["followers"] != ""]

    for cid, row in creators.items():
        if row["followers"] != "":
            continue
        likes = [p[0] for p in all_posts[cid] if p[0]]
        r, method = pick_ratio([], likes_follower_all, PRIOR_LIKES_PER_FOLLOWER, "likes/followers")
        if likes:
            row["followers"] = max(int(statistics.median(likes) / r), 100)
            mark("creators.csv", cid, "followers", "forecast",
                 f"median likes {statistics.median(likes):.0f} / {r:.4f} ({method})")

    for v in videos:
        cid = v["creator_id"]
        own = [(views, likes) for likes, views in all_posts.get(cid, [])]
        views_per_like, method = pick_ratio(own, [(b, a) for a, b in everyone], 1 / PRIOR_LIKES_PER_VIEW,
                                            "views/likes")
        followers = num(creators.get(cid, {}).get("followers"))
        if v["likes"] == "" and v["views"] != "":
            v["likes"] = int(num(v["views"]) / views_per_like)
            mark("videos.csv", v["video_id"], "likes", "forecast", f"views / {views_per_like:.1f} ({method})")
        elif v["likes"] == "" and followers:
            v["likes"] = int(followers * PRIOR_LIKES_PER_FOLLOWER)
            mark("videos.csv", v["video_id"], "likes", "forecast",
                 f"followers x {PRIOR_LIKES_PER_FOLLOWER} (typical Instagram ratio)")
        if v["views"] == "" and v["likes"] != "":
            v["views"] = max(int(num(v["likes"]) * views_per_like), int(num(v["likes"])))
            mark("videos.csv", v["video_id"], "views", "forecast", f"likes x {views_per_like:.1f} ({method})")
        if v["reach"] == "" and v["views"] != "":
            v["reach"] = max(int(num(v["views"]) * PRIOR_REACH_PER_VIEW), int(num(v["likes"]) or 0))
            mark("videos.csv", v["video_id"], "reach", "forecast",
                 f"views x {PRIOR_REACH_PER_VIEW} (typical unique-accounts/plays ratio)")

    # ---- derived creator columns
    for cid, row in creators.items():
        text = " ".join(v["caption"] for v in videos if v["creator_id"] == cid)
        profile = profiles.get(cid, {})
        if not row["niche"]:
            category = (profile.get("category") or "").lower()
            if any(w in category for w in GENERIC_CATEGORIES):
                category = ""
            row["niche"] = args.niche or guess_niche(text + " " + (profile.get("biography") or "")) or category
            if row["niche"]:
                mark("creators.csv", cid, "niche", "argument" if args.niche else "derived",
                     "" if args.niche else "caption/bio keywords")
        if not row["primary_language"]:
            row["primary_language"] = args.language or guess_language(text) or "es"
            mark("creators.csv", cid, "primary_language", "argument" if args.language else "derived",
                 "" if args.language else "caption words")
    # creators with no niche keyword get the most common niche: they come from the same campaign
    niches = [c["niche"] for c in creators.values() if c["niche"]]
    for cid, row in creators.items():
        if not row["niche"]:
            row["niche"] = max(set(niches), key=niches.count) if niches else "lifestyle"
            mark("creators.csv", cid, "niche", "derived", "most common niche in this dataset")

    # ---- 4. dummy for what Instagram never shows
    for cid, row in creators.items():
        if not row["follower_demographics_country"] and row["followers"] != "":
            row["follower_demographics_country"] = json.dumps(
                dummy_countries(int(num(row["followers"])), row["primary_language"], random.Random(cid)))
            mark("creators.csv", cid, "follower_demographics_country", "dummy")
    for v in videos:
        fake = dummy_video(v, durations.get(v["video_id"]), random.Random(v["video_id"]))
        for col, value in fake.items():
            if v[col] == "":
                v[col] = value
                mark("videos.csv", v["video_id"], col, "dummy")
        if v["total_interactions"] == "":
            v["total_interactions"] = sum(int(num(v[c]) or 0) for c in ["likes", "comments", "shares", "saved"])
            mark("videos.csv", v["video_id"], "total_interactions", "derived", "likes + comments + shares + saved")

    write_csv(out / "videos.csv", read_header(template / "videos.csv"), videos)
    write_csv(out / "comments.csv", read_header(template / "comments.csv"), comments)
    write_csv(out / "creators.csv", read_header(template / "creators.csv"), list(creators.values()))
    write_csv(out / "data_sources.csv", ["file", "id", "column", "source", "method"], sources)

    counts = {}
    for s in sources:
        counts[s["source"]] = counts.get(s["source"], 0) + 1
    print(f"Done: {len(videos)} videos, {len(comments)} comments, {len(creators)} creators -> {out}/")
    print("Filled cells: " + ", ".join(f"{n} {src}" for src, n in sorted(counts.items())))
    missing = sum(1 for v in videos if v["comments"] != "" and int(num(v["comments"])) >
                  sum(1 for c in comments if c["video_id"] == v["video_id"]))
    if missing:
        print(f"[info] {missing} video(s) have more comments on Instagram than were fetched")


if __name__ == "__main__":
    main()
