# instagram-video-downloader

Give it a list of Instagram post/reel URLs. For each one it downloads the video and thumbnail
and collects every metric Instagram will give. Results are written as CSVs with the same headers
as `../data-template`, so you can upload them straight into full-workflow-api.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # optional: cookies browser, Graph API token
```

## Run

```bash
.venv/bin/python ig_collect.py urls.txt                         # one URL per line
.venv/bin/python ig_collect.py urls.csv --out data              # CSV: url,campaign_id
.venv/bin/python ig_collect.py urls.txt --cookies-from-browser chrome --delay 8
```

Output in `--out` (default `output/`):

| File | Contents |
|---|---|
| `videos.csv` | one row per video: caption, posted_at, hours_since_post, views, likes, comments, shares, saved, reach… |
| `comments.csv` | one row per comment: text, timestamp, username, like_count |
| `media.csv` | file paths, duration, resolution, aspect ratio |
| `creators.csv` | handle, followers, follower countries |
| `videos/`, `thumbnails/` | the downloaded `.mp4` and `.jpg` |
| `raw/<shortcode>.json` | everything collected for the post, plus any errors |

Carousel posts that contain several videos get one row per video (`<shortcode>_1`, `_2`…).

## What you get, and where it comes from

| Data | Public post (no token) | Your own post (Graph API token) |
|---|---|---|
| Video + thumbnail | yes | yes |
| Caption, date, handle, duration, resolution | yes | yes |
| Likes, comment count | yes | yes |
| Views | sometimes; more often when logged in with cookies | yes |
| Comments | first page only (~15–50) | all, with replies |
| Followers of the creator | only with a token, and only for Business/Creator accounts | yes |
| Reach, shares, saved, total interactions, avg watch time | **no** | yes |
| Follower countries | **no** | yes, for your own account |
| Viewer countries per video, skip rate | **no** | **no**: Instagram shows these only inside the app |

Instagram does not expose shares, reach or audience countries for posts you don't own, through any
official channel. To get them for creators' posts, each creator has to connect their account (or
send you their Insights export). The script fills the cells it can and leaves the rest blank.

## Graph API setup (optional)

1. Convert the Instagram account to Business or Creator and link it to a Facebook Page.
2. Create an app at developers.facebook.com and add the Instagram Graph API product.
3. Generate a long-lived user token with `instagram_basic`, `instagram_manage_insights`,
   `instagram_manage_comments` and `pages_show_list`.
4. Get the IG user id: `GET /me/accounts?fields=instagram_business_account`.
5. Put `IG_ACCESS_TOKEN` and `IG_USER_ID` in `.env`.

## Tips

- If Instagram returns "login required" or "rate-limit", use `--cookies-from-browser` with a browser
  where you're logged into instagram.com, and raise `--delay`. A secondary account is safer.
- Only download content you have the right to use (your own or your campaign creators', with their
  permission). Automated scraping is against Instagram's Terms of Use.
