# Dataset templates (Section 7 evaluation)

Empty CSVs with the exact headers full-workflow-api expects, plus the new CAC columns.
Fill them in, then upload them all at once in the app's **Dataset** tab.

## Fill them in this order

| # | File | Required? | One row per | Uploaded to app? |
|---|---|---|---|---|
| 1 | campaigns.csv | yes | brand campaign | yes |
| 2 | creators.csv | yes | creator | yes |
| 3 | videos.csv | yes | video | yes |
| 4 | comments.csv | optional | comment | yes |
| 5 | media.csv | optional | video | yes |
| 6 | keyframes.csv | optional* | scene moment in a video | yes |
| 7 | transcripts.csv | optional* | spoken line in a video | yes |
| 8 | ground_truth.csv | for evaluation | video | yes (app hides it from the model) |
| 9 | ground_truth_media.csv | for unit tests | video | yes (hidden from the model) |
| 10 | manual_review.csv | for evaluation | video | **no**, keep it separate |

\* Needed when there is no real clip (no `video_url`). They are what the synthetic clip is built from.

**IDs must match across files.** `video_id` in every file must exist in videos.csv; `creator_id` and
`campaign_id` in videos.csv must exist in creators.csv / campaigns.csv. IDs: letters, digits, `_ . -` only.
Leave a cell **blank** when you don't know a value. Never put 0 for "unknown".

---

## 1. campaigns.csv
| Column | Example | Meaning |
|---|---|---|
| campaign_id | cmp_103 | unique id |
| brand | FitApp Pro | brand name |
| brand_category | fitness | category |
| **product_type** | subscription_app | `ecommerce` or `subscription_app` (**new**) |
| **target_cac** | 10.00 | the max cost per conversion the brand accepts, in USD (**new**) |
| target_countries | CO\|EC | countries separated by `\|` |
| campaign_clicks | 18420 | campaign total |
| campaign_views | 612000 | campaign total |
| click_ratio | 0.0301 | clicks ÷ views |

## 2. creators.csv
| Column | Example | Meaning |
|---|---|---|
| creator_id | cr_11 | unique id |
| handle | lucia.makes | social handle |
| niche | beauty | creator's niche |
| followers | 48200 | follower count |
| primary_language | es | `es` or `en` |
| follower_demographics_country | {"CO": 26400, "EC": 7100} | JSON: followers by country |
| link_clicks | 640 | optional: clicks tracked to this creator's own code or link in this campaign, all their videos together; blank when not tracked per creator (**new**) |

## 3. videos.csv: the early signals the model sees
| Column | Example | Meaning |
|---|---|---|
| video_id | vid_011 | unique id |
| creator_id | cr_01 | from creators.csv |
| campaign_id | cmp_101 | from campaigns.csv |
| media_product_type | REELS | REELS, VIDEO… |
| posted_at | 2026-08-28T14:10:00Z | when it was posted (UTC) |
| collected_at | 2026-08-31T14:10:00Z | when the metrics were read |
| hours_since_post | 72 | under 48 → the app defers to a human |
| caption | 3 pasos para… #Brand | post caption |
| reach | 100000 | under 1,000 → the app defers to a human |
| views | 142000 | |
| likes | 8200 | |
| comments | 310 | |
| shares | 1450 | |
| saved | 2100 | |
| total_interactions | 12060 | likes + comments + shares + saved |
| avg_watch_time_ms | 14200 | average watch time in milliseconds |
| skip_rate_pct | 22.5 | % who skipped in the first seconds |
| video_url | https://… | optional: real clip or TikTok/IG/YouTube link |

**Engagement baseline:** sort this file by `total_interactions ÷ reach`.

## 4. comments.csv
| Column | Example |
|---|---|
| comment_id | cmt_101 |
| video_id | vid_011 |
| text | Ya lo compré, llegó en 2 días |
| timestamp | 2026-08-28T15:02:00Z |
| username | maria_cvs |
| like_count | 184 |

## 5. media.csv
| Column | Example | Meaning |
|---|---|---|
| video_id | vid_011 | |
| media_path | media/vid_011/clip.mp4 | always this pattern |
| thumbnail_path | media/vid_011/thumb.png | always this pattern |
| duration_s | 28 | seconds |
| aspect_ratio | 9:16 | |
| resolution | 1080x1920 | |
| audio_type | voiceover+music | voiceover+music, music_only, voiceover |
| spoken_language | es | blank if nobody speaks |
| has_burned_captions | yes | yes / no |
| media_available | yes | yes / no |
| notes | | optional |

## 6. keyframes.csv: what the video shows
Times 0, 1, 2, 3 then every 3 s.
| Column | Example |
|---|---|
| keyframe_id | vid_011_kf00 |
| video_id | vid_011 |
| t_s | 0 |
| image_path | media/vid_011/kf_00.png |
| scene_description | Close-up of a woman holding the product, logo readable |
| on_screen_text | ¿Piel brillante con este calor? |

## 7. transcripts.csv: what the video says
| Column | Example |
|---|---|
| segment_id | vid_011_seg00 |
| video_id | vid_011 |
| start_s | 0 |
| end_s | 3 |
| language | es |
| text | ¿Tu piel brilla con este calor? Mira esto. |

## 8. ground_truth.csv: the locked answer key
**Fill this in and save a dated copy BEFORE running the pipeline. Never change it afterwards.**
| Column | Example | Meaning |
|---|---|---|
| video_id | vid_011 | |
| scenario | trap_high_engagement | label for your analysis (see list below) |
| expected_verdict | DO_NOT_SCALE | SCALE, DO_NOT_SCALE, DEFER_TO_HUMAN, FLAG_FRAUD |
| performed_well_after_scaling | no | **yes if cac ≤ the campaign's target_cac**, otherwise no; blank if never scaled |
| **ad_spend** | 500 | USD spent scaling it (**new**) |
| **conversions** | 25 | sales or sign-ups (**new**) |
| **cac** | 20.00 | ad_spend ÷ conversions (**new**) |
| roas | 0.61 | optional, keep if you have it |
| notes | Lots of laughs, no buying intent | why |

Scenario labels: `clear_winner`, `clear_loser`, `borderline`, `trap_high_engagement`
(high engagement, CAC above target), `trap_quiet_seller` (modest engagement, CAC below target),
`too_new`, `tiny_reach`, `missing_metrics`, `inorganic`, `audience_mismatch`.

## 9. ground_truth_media.csv: expected vision features (unit tests)
| Column | Example | Allowed values |
|---|---|---|
| video_id | vid_011 | |
| expected_hook_strength | strong | strong, medium, weak |
| expected_product_first_seen_s | 1 | seconds |
| expected_brand_match | yes | yes, partial, no |
| expected_cta_present | yes | yes / no |
| expected_face_on_screen | yes | yes / no |
| expected_injection_in_media | no | yes / no |
| expected_safety_issue | none | none or a short label |
| multimodal_expected_verdict | SCALE | same values as expected_verdict |
| notes | Hook at 0:00, CTA at 0:24 | |

## 10. manual_review.csv: your friend's blind decisions (NOT uploaded)
Filled in from videos.csv only, **before** anyone sees the AI scores.
| Column | Example | Meaning |
|---|---|---|
| video_id | vid_011 | |
| reviewer | friend_1 | |
| decision | SCALE | SCALE or DO_NOT_SCALE |
| reason | Strong saves and buying comments | one sentence (qualitative evidence) |
| minutes_spent | 4 | time spent on this video |

---

## Suggested mix for ~20 videos (at least 14 with outcomes)
- 2 campaigns per product type (ecommerce + subscription_app)
- 4 clear winners, 4 clear losers, 2 borderline
- 2 `trap_high_engagement` + 2 `trap_quiet_seller`
- 1 each: too_new, tiny_reach, missing_metrics, inorganic, audience_mismatch (no outcome needed)
