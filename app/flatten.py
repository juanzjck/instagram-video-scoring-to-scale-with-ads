"""Step 4 - merge. flatten() ported from the notebook, plus a 7th section: CONTENT FEATURES.

The first six sections are byte-for-byte what the notebook produced, so P5 here is the same
P5 that was evaluated there. Section 7 is appended only when `content` is passed.
"""

import json

from . import guard
from .config import COMMENT_LIMIT, NA
from .ingest import num, val


def fmt_int(x):
    v = num(x)
    return NA if v is None else f"{int(round(v)):,}"


def fmt_pct(x, dp=2):
    return NA if x is None else f"{x * 100:.{dp}f}%"


def fmt_ratio(x, dp=2):
    return NA if x is None else f"{x:.{dp}f}x"


def fmt_ts(t):
    """Seconds -> m:ss, the timestamp format P6 is asked to cite."""
    v = num(t)
    return NA if v is None else f"{int(v) // 60}:{int(v) % 60:02d}"


def div(a, b):
    a, b = num(a), num(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


def derived_metrics(row):
    """The five derived metrics. Any metric whose inputs are blank stays None."""
    return {
        "engagement_rate": div(row.get("total_interactions"), row.get("reach")),
        "save_rate":       div(row.get("saved"),              row.get("reach")),
        "share_rate":      div(row.get("shares"),             row.get("reach")),
        "views_per_reach": div(row.get("views"),              row.get("reach")),
        "views_per_hour":  div(row.get("views"),              row.get("hours_since_post")),
    }


def audience_breakdown(demographics_json, target_countries):
    """(target_share, top3, total); target_share is None when demographics are unavailable."""
    raw = val(demographics_json)
    if raw is None:
        return None, None, None
    try:
        dist = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, None, None
    total = sum(dist.values()) if dist else 0
    if not total:
        return None, None, None
    targets = [c.strip() for c in str(val(target_countries) or "").split("|") if c.strip()]
    in_target = sum(v for k, v in dist.items() if k in targets)
    top3 = sorted(dist.items(), key=lambda kv: kv[1], reverse=True)[:3]
    return in_target / total, top3, total


def _yn(x):
    return NA if x is None else ("yes" if x else "no")


def _issue_detail(issue):
    """The vision model quotes what it saw. For an injection that quote IS the payload, so
    the scorer is told one was found, not what it said."""
    if issue.get("type") == "prompt_injection":
        return " (text addressed to the reviewer; content withheld by the injection guard)"
    return f" ({guard.redact(issue['detail'])})" if val(issue.get("detail")) else ""


def content_section(content):
    """Section 7. `content` is {"preprocess": ..., "features": ...}; either may be None.

    Every blank renders as `not available` - the same rule the engagement metrics follow.
    Frame notes, on-screen text and the transcript are untrusted and sit inside delimiters.
    """
    pre = (content or {}).get("preprocess") or {}
    f = (content or {}).get("features") or {}
    L = ["", "=== CONTENT FEATURES ==="]
    L.append(f"Media source: {val(pre.get('source')) or NA}")
    dur = num(pre.get("duration_s"))
    L.append(f"Duration: {NA if dur is None else f'{dur:.0f} seconds'}")
    L.append(f"Spoken language: {val(pre.get('language')) or NA}")

    hook = val(f.get("hook_strength"))
    L.append(f"Hook strength (first 3 seconds): {hook or NA}"
             + (f" - {guard.defuse(f['hook_reason'])}" if hook and val(f.get("hook_reason")) else ""))

    visible = f.get("product_visible")
    first = num(f.get("product_first_seen_s"))
    if visible is None:
        L.append(f"Product first appears: {NA}")
    elif not visible or first is None:
        L.append("Product first appears: never shown in the sampled frames")
    else:
        L.append(f"Product first appears: {fmt_ts(first)}")

    bm = val(f.get("brand_match"))
    L.append(f"Content matches the brand: {bm or NA}"
             + (f" - {guard.defuse(f['brand_match_reason'])}" if bm and val(f.get("brand_match_reason")) else ""))

    cta = f.get("cta_present")
    line = f"Call to action: {_yn(cta)}"
    if cta and num(f.get("cta_t_s")) is not None:
        line += f" at {fmt_ts(f['cta_t_s'])}"
    L.append(line)
    L.append(f"Face on screen: {_yn(f.get('face_on_screen'))}")

    issues = f.get("safety_issues")
    if issues is None:
        L.append(f"Safety problems: {NA}")
    elif not issues:
        L.append("Safety problems: none found")
    else:
        L.append("Safety problems: " + "; ".join(
            f"{i.get('type', 'other')}"
            + (f" at {fmt_ts(i['t_s'])}" if num(i.get("t_s")) is not None else "")
            + _issue_detail(i)
            for i in issues))

    frames = f.get("frames") or pre.get("frames") or []
    L.append("Keyframes (AI notes on sampled frames):")
    if not frames:
        L.append(NA)
    else:
        L.append("<keyframes>")
        for fr in frames:
            note = val(fr.get("note")) or val(fr.get("scene_description"))
            ost = val(fr.get("on_screen_text"))
            L.append(f"[{fmt_ts(fr.get('t_s'))}] {guard.defuse(note) if note else NA}"
                     f" | on-screen text: {guard.redact(ost) if ost else 'none'}")
        L.append("</keyframes>")

    transcript = pre.get("transcript")
    L.append("Transcript (speech-to-text):")
    if transcript is None:
        L.append(NA)
    elif not transcript:
        L.append("no speech detected")
    else:
        L.append("<transcript>")
        for s in transcript:
            L.append(f"[{fmt_ts(s['start_s'])}-{fmt_ts(s['end_s'])}] {guard.redact(s['text'])}")
        L.append("</transcript>")
    return L


def flatten(video_row, comments=None, content=None):
    """Render one video as the plain-text summary given to the model.

    content=None  -> the notebook's six sections (what P5 reads).
    content={...} -> the same six plus CONTENT FEATURES (what P6 reads).
    """
    r = dict(video_row)
    d = derived_metrics(r)
    L = []

    L.append("=== VIDEO ===")
    L.append(f"Video ID: {val(r.get('video_id')) or NA}")
    hrs = num(r.get("hours_since_post"))
    L.append(f"Hours since posted: {NA if hrs is None else f'{hrs:.0f}'}")
    L.append(f"Caption: {val(r.get('caption')) or NA}")
    L.append("Note: Instagram insights can lag up to 48h.")

    L.append("")
    L.append("=== RAW METRICS ===")
    L.append(f"Reach: {fmt_int(r.get('reach'))}")
    L.append(f"Views: {fmt_int(r.get('views'))}")
    L.append(f"Likes: {fmt_int(r.get('likes'))}")
    L.append(f"Comments (API total): {fmt_int(r.get('comments'))}")
    L.append(f"Shares: {fmt_int(r.get('shares'))}")
    L.append(f"Saved: {fmt_int(r.get('saved'))}")
    L.append(f"Total interactions: {fmt_int(r.get('total_interactions'))}")
    watch_ms = num(r.get("avg_watch_time_ms"))
    L.append(f"Average watch time: {NA if watch_ms is None else f'{watch_ms / 1000:.1f} seconds'}")
    skip = num(r.get("skip_rate_pct"))
    L.append(f"Skip rate: {NA if skip is None else f'{skip:.1f}%'}")

    L.append("")
    L.append("=== DERIVED METRICS ===")
    L.append(f"Engagement rate (total interactions / reach): {fmt_pct(d['engagement_rate'])}")
    L.append(f"Save rate (saved / reach): {fmt_pct(d['save_rate'])}")
    L.append(f"Share rate (shares / reach): {fmt_pct(d['share_rate'])}")
    L.append(f"Views per reach (views / reach): {fmt_ratio(d['views_per_reach'])}")
    vph = d["views_per_hour"]
    L.append(f"Views per hour (views / hours since posted): "
             f"{NA if vph is None else f'{vph:,.0f}'}")

    L.append("")
    L.append("=== CAMPAIGN ===")
    L.append(f"Brand: {val(r.get('brand')) or NA}")
    L.append(f"Brand category: {val(r.get('brand_category')) or NA}")
    tgt = val(r.get("target_countries"))
    L.append(f"Target countries: {tgt.replace('|', ', ') if tgt else NA}")
    L.append(f"Campaign clicks: {fmt_int(r.get('campaign_clicks'))}")
    L.append(f"Campaign views: {fmt_int(r.get('campaign_views'))}")
    cr = num(r.get("click_ratio"))
    L.append(f"Campaign click ratio: {NA if cr is None else f'{cr * 100:.2f}%'} "
             f"(campaign-level, aggregated across all creators in the campaign)")

    L.append("")
    L.append("=== CREATOR ===")
    L.append(f"Niche: {val(r.get('niche')) or NA}")
    L.append(f"Followers: {fmt_int(r.get('followers'))}")
    L.append(f"Primary language: {val(r.get('primary_language')) or NA}")
    share, top3, total = audience_breakdown(r.get("follower_demographics_country"),
                                            r.get("target_countries"))
    if share is None:
        L.append("Audience share in campaign target countries: audience data not available")
        L.append("Top 3 audience countries: audience data not available")
    else:
        L.append(f"Audience share in campaign target countries: {fmt_pct(share)}")
        L.append("Top 3 audience countries: " +
                 ", ".join(f"{c} {fmt_pct(n / total)}" for c, n in top3))
    clicks = num(r.get("link_clicks"))
    if clicks is not None:                   # optional: only shown when tracked, so P5 is unchanged otherwise
        n = int(num(r.get("creator_videos")) or 1)
        L.append(f"Creator link clicks: {fmt_int(clicks)} (this creator's own code or link, "
                 f"across all {n} of their video{'s' if n != 1 else ''} in the campaign)")
        ccr = div(clicks, r.get("creator_views"))
        L.append(f"Creator click ratio (link clicks / views of those videos): {fmt_pct(ccr)}")

    L.append("")
    L.append("=== COMMENTS (sample, not the full set) ===")
    if comments is None or len(comments) == 0:
        L.append("no comments available")
    else:
        L.append("<comments>")
        for _, c in comments.head(COMMENT_LIMIT).iterrows():
            text = str(val(c.get("text")) or "").replace("\n", " ").strip()
            likes = fmt_int(c.get("like_count"))
            L.append(f"{text} ({likes} likes)")
        L.append("</comments>")

    if content is not None:
        L.extend(content_section(content))

    return "\n".join(L)
