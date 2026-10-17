# PartGram scale review — full workflow API

Multimodal extension of the prompt-engineering notebook: a FastAPI service that takes a
creator video from raw media to a logged human decision, with a review-card UI.

## Run

```bash
# from the repo root, using the existing venv
.venv/bin/pip install -r full-workflow-api/requirements.txt
cd full-workflow-api
../.venv/bin/python scripts/make_synthetic_media.py     # once: builds data-example/media/*/clip.mp4
../.venv/bin/uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 (UI) or http://127.0.0.1:8000/docs (API).

### Your own dataset

The **Dataset** tab is the starting point:

1. **Upload** the CSVs (select them all at once). Required: `videos.csv`, `creators.csv`,
   `campaigns.csv`. Optional: `comments.csv`, `media.csv`, `keyframes.csv`, `transcripts.csv`,
   `ground_truth.csv`, `ground_truth_media.csv`. Each upload is stored in `uploads/<timestamp>/`
   with its own `runs/` folder, and becomes the active dataset.
2. **Video URLs**: one per video, typed in the table or pre-filled from a `video_url` column in
   `videos.csv` or `media.csv`. A direct link to a media file is downloaded as-is; a page link
   (Instagram, TikTok, YouTube) goes through `yt-dlp`. The download lands in
   `runs/<video_id>/clip.mp4`. Changing a URL re-downloads and re-scores that video.
3. **Start pipeline** runs every video in the background; the table shows each video's step.

Same thing over the API: `POST /api/dataset` (multipart `files`), `PUT /api/videos/{id}/url`,
`POST /api/pipeline/start`, `GET /api/pipeline/status`.

`ANTHROPIC_API_KEY` is read from `full-workflow-api/.env`, falling back to
`FirstTest-prompts/.env`. No system packages are needed: ffmpeg ships inside
`imageio-ffmpeg`, and the Whisper model (~150 MB) downloads on first use.

## Pipeline

| # | Step | Where | Notes |
|---|---|---|---|
| 1 | Ingest | `app/ingest.py` | CSVs joined as in the notebook, plus `media.csv` and each video's media folder. Blank = `None`, never 0. |
| 2 | Preprocess | `app/media.py` | Keyframes at 0, 1, 2, 3 s then every 3 s (ffmpeg). Timestamped transcript from local Whisper. |
| 3 | Visual analysis | `app/vision.py` | One Claude vision call per video, JSON-schema output: hook strength, product first seen, brand match, CTA, safety issues, plus a note and the on-screen text for every frame (this is the OCR). |
| 4 | Merge | `app/flatten.py` | `flatten()` ported unchanged; a 7th section `=== CONTENT FEATURES ===` is appended. Blank features render `not available`. |
| 6 | Score | `app/prompts.py`, `app/scoring.py` | P5 verbatim from the notebook. P6 is derived from P5 by string surgery (cannot drift). Reasons cite `m:ss`. |
| — | Rule guard | `app/scoring.py` | Applied **in code after the model answers**: under 48 h, reach under 1,000, or a missing core metric → `DEFER_TO_HUMAN`. The model's own verdict is kept alongside. |
| 7 | Injection guard | `app/guard.py` | Transcript, on-screen text and comments are delimited, delimiter look-alikes are defused, a regex scan flags instruction-like text, and flagged media text is redacted before the scoring prompt. |
| 8 | Review card | `app/static/` | Player with the transcript as a captions track, keyframe strip with AI notes, score + range, engagement chart, spoken summary (browser speech synthesis). |
| 9 | Human decision | `POST /api/videos/{id}/decision` | Scale / don't scale / override (note required). Appended to `runs/decisions.jsonl` with reviewer, view mode and time taken. |
| 10 | Campaign recap | `app/recap.py`, `POST /api/campaigns/{id}/recap` | ~45 s mp4 per campaign: top-scored video + one do-not-scale example, cut at the timestamps P6 cited; Claude narration (numbers checked against the data in code); voice from `app/genmedia.py` (OpenAI neural TTS when `OPENAI_API_KEY` is set, macOS `say` otherwise); score/verdict overlays; reach / save-rate chart; burned-in captions + `.vtt`. Output in `runs/recaps/<campaign_id>/`. |
| — | Evaluate | `app/evaluate.py` | P5 vs P6 vs ground truth, vision features vs `ground_truth_media.csv`, reviewer time with vs without the card. The only module that opens the answer keys. |

## API

| Method | Path | |
|---|---|---|
| GET | `/api/videos` | ingested videos and pipeline status |
| POST | `/api/videos/{id}/process?force=` | run steps 2–7, returns the card |
| POST | `/api/process-all` | same, every video |
| GET | `/api/videos/{id}/card` | review card payload |
| GET | `/api/videos/{id}/summary?multimodal=` | the `flatten()` text (6 or 7 sections) |
| GET | `/api/videos/{id}/captions.vtt` | transcript as WebVTT |
| POST | `/api/videos/{id}/decision` | log the human decision |
| GET | `/api/decisions` | the decision log |
| GET | `/api/evaluate?run_missing=` | evaluation report |
| GET | `/api/campaigns` | campaigns, scored counts, recap if generated |
| POST | `/api/campaigns/{id}/recap?force=&language=` | build the recap video; language defaults to the campaign's own (`es` or `en`), same voice as its videos |
| GET | `/api/campaigns/{id}/recap` | recap metadata and file URLs |

Each stage is cached as JSON in `runs/<video_id>/`; `force=true` recomputes.

## Generated media

Claude has no image or audio output. Two things are generated anyway (`app/genmedia.py`):

- **Example clip pictures - Claude.** `python scripts/make_synthetic_media.py --ai-images --force vid_001`
  has Claude draw each keyframe's scene description as an SVG illustration, rendered to PNG with
  `resvg-py`. The vision step then looks at a picture instead of reading a sentence. No extra key.
  They are drawings, not photographs; product labels are exact because SVG text is real text.
  On-screen text is still laid over the picture in code so it matches the answer key. Pictures and
  their `.svg` source are cached in `data-example/media/<video_id>/ai/claude/`. Re-run the pipeline
  for that video afterwards. `--ai-images=openai` uses a raster image model instead (needs
  `OPENAI_API_KEY`).
- **Recap voice.** OpenAI neural text-to-speech when `OPENAI_API_KEY` is in `.env`, the macOS `say`
  command otherwise. Models and voice are set in `.env`, see `.env.example`.

Charts, score badges and captions are never generated: they must be exact, so they stay in Pillow.

## Things worth knowing

- **Claude does not take audio in or produce audio out.** Transcription is local Whisper;
  the spoken summary is the browser's `speechSynthesis`.
- **The clips are synthetic.** `data-example` ships no media, so
  `scripts/make_synthetic_media.py` renders each `keyframes.csv` row as a still (scene
  description as text, on-screen text as a burned-in caption) and voices `transcripts.csv`
  with macOS `say`. The extraction, transcription and vision steps are real, but the vision
  model is reading a written description of the scene, not seeing it. Feature accuracy on
  these clips is therefore an upper bound. Real `clip.mp4` files dropped into
  `data-example/media/<video_id>/` are used as-is.
- **48 h, not 24 h.** P5 (unchanged, for a fair baseline) defers under roughly 24 h. P6 and
  the code-level rule guard use 48 h.
- **Score range** is stated by the model in P6 (`SCORE_RANGE`). It is a self-report, not a
  statistical interval.
- **Reviewer timing** needs decisions in both modes: use the Card / Text only toggle in the
  header. Until both have data the comparison is empty.
- **Model** comes from `LLM_MODEL` (default `claude-opus-5-5`). No sampling parameters are
  sent, so run-to-run variation is whatever the model's default is.
