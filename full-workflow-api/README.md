# PartGram scale review — full workflow API

Multimodal extension of the prompt-engineering notebook: a FastAPI service that takes a
creator video from raw media to a logged human decision, with a review-card UI.

## Run

```bash
# from the repo root, using the existing venv
.venv/bin/pip install -r full-workflow-api/requirements.txt
cd full-workflow-api
../.venv/bin/uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 (UI) or http://127.0.0.1:8000/docs (API).

### Building the dataset

No dataset is bundled: **every server start is clean**, with no session open.
The **Dataset** tab is the starting point.

**Sessions.** A session is **one campaign**, in one folder, `uploads/<id>/`, with a JSON memory file `session.json`
(name, created / updated / generated times, and every row typed in the form). It is created the
moment you type in the empty form and autosaved as you go (and on tab close). The same folder holds
the generated CSVs and `runs/` (video URLs, clips, pipeline results, recaps, `decisions.jsonl`),
so nothing is lost. **New session** closes the current one; the session list reopens any saved
one with its form and results: each saved session is a toggle that expands to show its brands, row counts, scored videos, decisions, recaps and answer keys, with an Open button. Older dataset folders without `session.json` are listed too, their
form rebuilt from their CSVs.

1. **Form**: the campaign first, as a single record (campaign ID and brand required; the session
   is named "brand · campaign ID" after it). Once those two are filled in, the campaign's
   creators, videos and comments open as editable tables, same columns as `../data-template/`.
   Videos take the campaign ID automatically. CSV import keeps one campaign (the open one, or the
   file's first) with its videos, their creators and comments; other campaigns go in new sessions.
   Comments have a **Bulk add** box: pick a video and paste one comment per line, or rows copied
   from a spreadsheet (tab-separated: text, username, likes, posted at). Media details, keyframes
   and transcripts are not entered: the pipeline produces them from each video's clip. Creator / campaign / video columns are dropdowns fed by the ids typed in the other
   tables. "Import from CSV files…" fills the tables from CSVs shaped like the templates.
   `comment_id` is filled in by the server; `click_ratio`, `hours_since_post` and `total_interactions` are
   computed when left blank and their inputs are present. **Generate dataset** validates every
   row (types, required cells, duplicate ids, ids that do not match) and marks problems in red;
   on success it writes the CSVs into the session folder. **Regenerating** replaces them and
   drops cached pipeline results and recaps (they came from the old rows); clips and decisions stay.
2. **Videos**: per video, a URL (direct media link, or Instagram / TikTok / YouTube page via
   `yt-dlp`) or an uploaded video file. Without either, the video is scored on its metrics and
   comments only.
3. **Start pipeline** runs every video in the background; the table shows each video's step.

### Ground truth (Evaluation tab)

The **Evaluation** tab has two parts. **Ground truth**: for the open campaign, one row per video
with the real results (columns of `data-template/ground_truth.csv`: scenario, expected verdict,
performed well, ad spend, conversions, CAC, ROAS, notes). It autosaves as a draft in
`session.json`. Left blank, CAC is ad spend ÷ conversions and "performed well" is CAC ≤ the
campaign's target CAC. Videos with no results are left out. **Lock ground truth** (behind a
confirmation checkbox) validates it, writes `ground_truth.csv` plus a dated copy
`ground_truth.locked-<UTC time>.csv`, and records the lock time, the file's SHA-256 and how many
videos were already scored (lock before looking at scores). After that it is read-only: the API
refuses changes (409) and `ground_truth.csv` cannot be uploaded. **Results** measures P5 / P6
against the locked file; `ground_truth_media.csv` (expected vision features) is still an optional
upload there. `manual_review.csv` never enters the app.

API: `GET|PUT /api/ground-truth`, `POST /api/ground-truth/lock` (body `{"rows": [{"video_id": …, …}]}`).

API: `GET /api/dataset/schema`, `GET /api/sessions`, `GET|PUT /api/session` (autosave, same body as
below), `POST /api/sessions/{id}/open`, `POST /api/dataset` (JSON `{"tables": {"videos.csv": [{...}], ...},
"name": "…"}`), `POST /api/dataset/reset` (new session), `POST /api/dataset/answer-keys` (multipart `files`),
`POST /api/videos/{id}/clip` (multipart `file`), `PUT /api/videos/{id}/url`,
`POST /api/pipeline/start`, `GET /api/pipeline/status`.

`ANTHROPIC_API_KEY` is read from `full-workflow-api/.env`, falling back to
`FirstTest-prompts/.env`. Claude is the default LLM; see [Choosing the LLM](#choosing-the-llm) for
OpenAI, Gemini and local models. No system packages are needed: ffmpeg ships inside
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

| GET / PUT | `/api/llm` | provider + model of the open session (see Choosing the LLM) |
| GET | `/api/llm/models?provider=` | models a provider offers now, with vision support when known |
| GET / POST | `/api/prompts` | prompt versions + active id + adoption log / save a new version `{"parent", "text", "note"}` |
| GET / DELETE | `/api/prompts/{id}` | one version with its text / delete a custom, non-active version |
| GET | `/api/prompts/{id}/diff?against=` | unified diff against its parent (or another version) |
| GET | `/api/prompts/{id}/adoption` | what the adoption gate says now, without adopting |
| PUT | `/api/prompts/active` | adopt `{"id", "override_reason"}`; 409 unless it beat P5 or a reason is given |
| GET / POST | `/api/iterations` | iterations, newest first, each compared with P5 / start one `{"prompt_id", "force", "note"}` |
| GET | `/api/iterations/{n}` | one iteration with its frozen verdicts and comparison |

Each stage is cached as JSON in `runs/<video_id>/` (model answers in `runs/<video_id>/llm/<provider>--<model>/`); `force=true` recomputes.

## Choosing the LLM

Every model call goes through `app/llm.py`. Providers: `anthropic` (default), `openai`, `gemini`,
`ollama` (local, native API) and `openai_compatible` (any local OpenAI-style server: LM Studio,
llama.cpp, vLLM).

- **Env default:** `LLM_PROVIDER` plus `LLM_MODEL` (the model for that provider). Per-provider defaults:
  `ANTHROPIC_MODEL` (default `claude-opus-5-5`), `OPENAI_MODEL`, `GEMINI_MODEL`, `OLLAMA_MODEL`,
  `LOCAL_LLM_MODEL`. Keys and endpoints: `OPENAI_API_KEY`, `GEMINI_API_KEY` (or `GOOGLE_API_KEY`),
  `OLLAMA_BASE_URL` (default `http://localhost:11434`), `OLLAMA_NUM_CTX` (default 16384),
  `LOCAL_LLM_BASE_URL` (default `http://localhost:1234/v1`, LM Studio), `LOCAL_LLM_API_KEY`.
- **Per session (UI):** Dataset tab → *4 · Run* → provider + model, then *Use this model*. The model
  list comes from the provider (`GET /api/llm/models?provider=`; Ollama models are flagged by vision
  support). Saved in `session.json` as `"llm"`; it wins over the env default. API: `GET /api/llm`,
  `PUT /api/llm {"provider", "model"}` (an empty body resets to the env default; refused while the
  pipeline runs).
- **Cache per model.** Features, P5 and P6 answers live in `runs/<video_id>/llm/<provider>--<model>/`,
  so switching models never reuses another model's answers, and switching back costs nothing. The
  Review and Evaluation tabs show the chosen model's results; `evaluate` returns `"llm"`, each stage
  stores `provider` + `model` in its `usage`, and the export's `results.csv` has `p6_provider` /
  `p6_model`. Files from before this change (`runs/<video_id>/score_p6.json`) are still read for the
  Claude model that wrote them. Preprocessing (keyframes, Whisper) and `timing.json` are shared.
- **Vision.** The keyframe call needs a model that reads images. Ollama models without the `vision`
  capability fail with a clear error before any call (pull one, e.g. `ollama pull qwen2.5vl`); OpenAI and
  local OpenAI-compatible models that cannot read images fail with the provider's own error. Nothing
  is skipped silently. Example datasets without pixels (frame descriptions only) work on any model.
- **Safety stays in code.** Refusals (Anthropic `refusal`, OpenAI `refusal`/`content_filter`, Gemini
  safety blocks) raise; JSON replies are parsed strictly and checked against the schema; the rule guard
  and injection guard run on every provider's answer. No sampling parameters are sent to any provider.
- **SDKs are optional.** `openai` and `google-genai` are imported only when used; without them the
  app starts and that provider says which package to install.

## Prompt versions and iterations

The tuning loop of the workflow design (Section 6): after a campaign the data/AI team reads the error
report (or the prompt pack), hand-tunes P6, and a new version is adopted only if it beats P5.
Evaluation tab → *Prompt versions*.

- **Versions** (`app/prompt_versions.py`) live in `session.json` under `"prompts"`. Built in: `P5`
  (fixed baseline, text only, never edited) and `P6` (derived from P5 in `prompts.py`, the default).
  *New version from this* saves `P6.2`, `P6.3`, … from a parent: immutable once saved, SHA-256
  recorded, ids never reused. A template must contain `{video_summary}` exactly once and double any
  other literal brace. Every version is P6-family (gets the multimodal summary).
- **Cache per version.** Answers go to `runs/<video_id>/llm/<provider>--<model>/score_<id>.json`
  (`score_p6.json`, `score_p6.2.json`, …) and carry `prompt_id` + `prompt_sha256`; an answer from a
  different text is never reused.
- **Iterations** (`app/iterations.py`): *Run iteration* scores every video with the version **and**
  with P5 on the current provider + model (a background job, polled at `/api/pipeline/status`, kind
  `iteration`), then freezes each video's final verdicts in `runs/iterations/<n>.json`. The comparison
  is computed by `evaluate.compare_iteration` against the ground truth: the real CAC outcome when any
  video has one, else the expected verdict, as scale / don't scale.
- **Adoption gate.** A version beats P5 when it has no more false SCALE (budget on a loser) and then
  higher accuracy, or equal accuracy with fewer false SCALE or more winners caught, on the latest
  finished iteration on the **current model**. Otherwise `PUT /api/prompts/active` returns 409 unless
  a written override reason (≥ 10 characters) is given; every adoption is logged with the gate's answer.
  `P6` can always be restored. The adopted version drives the review card, the decision log
  (`ai_prompt`), the evaluation (`"prompt"`), the recap, the export (`p6_prompt`) and the prompt pack.

## Generated media

Claude has no image or audio output. Two things are generated anyway (`app/genmedia.py`):

- **Example clip pictures - Claude.** `python scripts/make_synthetic_media.py --dataset=<id> --ai-images --force vid_001`
  has Claude draw each keyframe's scene description as an SVG illustration, rendered to PNG with
  `resvg-py`. The vision step then looks at a picture instead of reading a sentence. No extra key.
  They are drawings, not photographs; product labels are exact because SVG text is real text.
  On-screen text is still laid over the picture in code so it matches the answer key. Pictures and
  their `.svg` source are cached in `uploads/<id>/media/<video_id>/ai/claude/`. Re-run the pipeline
  for that video afterwards. `--ai-images=openai` uses a raster image model instead (needs
  `OPENAI_API_KEY`).
- **Recap voice.** OpenAI neural text-to-speech when `OPENAI_API_KEY` is in `.env`, the macOS `say`
  command otherwise. Models and voice are set in `.env`, see `.env.example`.

Charts, score badges and captions are never generated: they must be exact, so they stay in Pillow.

## Things worth knowing

- **Claude does not take audio in or produce audio out.** Transcription is local Whisper;
  the spoken summary is the browser's `speechSynthesis`.
- **Synthetic clips.** For a dataset without real videos,
  `scripts/make_synthetic_media.py --dataset=<id>` (id shown in the Dataset tab) renders each `keyframes.csv` row as a still (scene
  description as text, on-screen text as a burned-in caption) and voices `transcripts.csv`
  with macOS `say`. The extraction, transcription and vision steps are real, but the vision
  model is reading a written description of the scene, not seeing it. Feature accuracy on
  these clips is therefore an upper bound. Real clips uploaded in the Dataset tab are used as-is.
- **48 h, not 24 h.** P5 (unchanged, for a fair baseline) defers under roughly 24 h. P6 and
  the code-level rule guard use 48 h.
- **Score range** is stated by the model in P6 (`SCORE_RANGE`). It is a self-report, not a
  statistical interval.
- **Reviewer timing** needs decisions in both modes: use the Card / Text only toggle in the
  header. Until both have data the comparison is empty.
- **Model** comes from the session's choice, else `LLM_PROVIDER` / `LLM_MODEL` (default Anthropic
  `claude-opus-5-5`). No sampling parameters are sent, so run-to-run variation is whatever the
  model's default is.

## Command line and tests

`scripts/score_video.py` runs the same pipeline without the web UI: input a session id and video
ids, it calls the vision and scoring APIs, applies the rule guard, prints each verdict and saves
`runs/cli_results.json`. Cached stages are reused unless `--force`; `--summary` prints the text
the model receives without calling the API.

```bash
python scripts/score_video.py --list
python scripts/score_video.py <session_id> --all
```

`tests/` checks the integration offline (fake Anthropic client, no key, no network): the request
sent, structured-output requests, refusal handling, response parsing, the rule guard and the
injection guard. `pip install pytest`, then `python -m pytest -q tests` (20 tests).
