# PartGram scale review — full workflow API

Multimodal extension of the prompt-engineering notebook: a FastAPI service that takes a
creator video from raw media to a logged human decision, with a review-card UI.

## Run

```bash
# from the repo root, using the existing venv
.venv/bin/pip install -r full-workflow-api/requirements.txt
cd full-workflow-api
.venv/bin/uvicorn app.main:app --reload
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
   creators, videos and comments open as editable tables, same columns as `old/data-template/`.
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
with the real results (columns of `old/data-template/ground_truth.csv`: scenario, expected verdict,
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
`old/FirstTest-prompts/.env`. Claude is the default LLM; see [Choosing the LLM](#choosing-the-llm) for
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
| GET / POST | `/api/prompts` | the global prompt library (+ the open session's active id and adoption log, null with no session open) / save a new version `{"parent", "text", "note"}` |
| GET / DELETE | `/api/prompts/{id}` | one version with its text / delete a custom, non-active version |
| GET | `/api/prompts/{id}/diff?against=` | unified diff against its parent (or another version) |
| GET | `/api/prompts/{id}/adoption` | what the adoption gate says now, without adopting |
| PUT | `/api/prompts/active` | adopt `{"id", "override_reason"}`; 409 unless it beat P5 or a reason is given |
| GET / POST | `/api/iterations` | iterations, newest first, each compared with P5 / start one `{"prompt_id", "force", "note"}` |
| GET | `/api/iterations/{n}` | one iteration with its frozen verdicts and comparison |

Each stage is cached as JSON in `runs/<video_id>/` (model answers in `runs/<video_id>/llm/<provider>--<model>/`); `force=true` recomputes.

## Choosing the LLM

Every model call goes through `app/llm.py`. Providers: `anthropic` (default), `openai`, `gemini`,
`ollama` (local, native API), `openai_compatible` (any local OpenAI-style server: LM Studio,
llama.cpp, vLLM) and `claude_code` (the Claude Code CLI on the user's own subscription login, below).

In the Dataset tab, next to *Start pipeline*, a **Run on** choice picks between *This machine: Claude Code
(subscription)* and *Anthropic API* (saved per session). An option that cannot run is disabled with the reason
(CLI not found, `ANTHROPIC_API_KEY` not set; only the variable's name is shown, never the key); no model call is
made to check, so a CLI login problem only shows on the first run. Other providers stay in the picker below it.

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

### Claude Code CLI (`claude_code`)

Runs each call as `claude -p` in a subprocess, on the login of the `claude` CLI (a Claude
subscription), not an API key. Settings: `CLAUDE_CODE_MODEL` (default `claude-opus-5-5`),
`CLAUDE_CODE_BIN` (default: `claude` on `PATH`, else `~/.local/bin/claude`), `CLAUDE_CODE_TIMEOUT_S`
(per call, default 600), `CLAUDE_CODE_RETRIES` (default 4) and `CLAUDE_CODE_MAX_WAIT_S` (default 900).

- **Clean context.** This machine's Claude Code has user hooks (e.g. a "caveman mode"
  SessionStart / UserPromptSubmit hook), plugins, CLAUDE.md files and memory. None of it may reach
  a scoring call. `--bare` would be the clean switch, but it ignores the subscription login, so the
  call uses `--safe-mode --setting-sources "" --strict-mcp-config --tools "" --disable-slash-commands
  --no-session-persistence`, replaces Claude Code's agent prompt with `--system-prompt` (empty when the
  caller has none), runs in an empty temp directory and passes only `PATH`, `HOME`, `USER`, `TMPDIR`,
  `LANG` and the like: no `ANTHROPIC_API_KEY` (an API key would win over the login) and no
  `CLAUDECODE` / `CLAUDE_CODE_*` of a parent session. Checked with a probe prompt asking the model to
  list everything it was given. Without the flags it lists the caveman hooks, memory instructions,
  10 tools and the skills list. With them it lists no hooks, CLAUDE.md, memory, skills or tools
  (`system/init` reports `tools: []`, `mcp_servers: []`, `skills: []`). What remains is added by the CLI
  itself and no flag removes it while the subscription login is used: the one-line identity
  "You are a Claude agent, built on Anthropic's Claude Agent SDK.", a reminder carrying the account's
  e-mail, and an environment block (the temp cwd, OS, date, model id). None of it is about the video.
- **Images.** The message goes in on stdin with `--input-format stream-json`, so the keyframes are
  ordinary base64 image blocks, exactly as for the API. JSON-schema calls (vision) add `--json-schema`;
  the CLI answers through its `StructuredOutput` tool and `structured_output` is then parsed strictly
  against the schema as for every provider.
- **Errors.** An error result, an empty answer, a refusal, a missing login (`ProviderUnavailable`) or a
  timeout raises. Rate limits and overload (429, 5xx, "usage limit") are retried with backoff
  (30 s, 60 s, …); a usage limit that resets later than `CLAUDE_CODE_MAX_WAIT_S` raises `RateLimited` at
  once so a batch can stop rather than sleep for hours. `max_tokens` is not sent (the CLI has no such
  flag), and there are no sampling flags.
- **Usage.** Each stage's `usage` gets the tokens (input includes cache reads and writes), latency and
  `cost_usd`: the CLI's list-price figure. A subscription is not billed per call; it has a 5-hour and a
  7-day usage window instead.

## Collections (many runs of one campaign)

A **collection** is a parent that groups copies ("runs") of one saved session, to repeat the whole
workflow on the same campaign and measure how stable the verdicts are (`app/collection.py`). Each run is
an ordinary session made by *Copy session* (`dataset.copy_session`, an APFS copy-on-write clone on
macOS, so 100 runs of a 100 MB session take almost no disk), named "<collection> · run 001", with
`"collection": {"id", "name", "index"}` in its `session.json`. The source is only read. The parent lives in
`uploads/_collections/<id>/collection.json`, next to the batch's `test_summary.json`, `report.json` /
`report.md`, `shared_features/` and `logs/`. The app opens on the **Sessions** screen: parent
sessions (collections, with their child runs inside) and sessions on their own, plus a form to make a parent
from a saved session. *Open dashboard* on a parent shows the evaluation of all its children (mean, min, max
per prompt, one row per child); *Open* on a child, or on a single session, goes inside it, where Dataset,
Review, Evaluation and Recap are sub-tabs under a breadcrumb. A copy of a run is a plain session again.

API: `GET /api/collections`, `POST /api/collections {"source", "name", "children"}`,
`GET /api/collections/{id}`, `GET /api/collections/{id}/evaluation` (every run evaluated from cached
scores, plus the mean across runs; the parent's dashboard), `POST /api/collections/{id}/children {"n"}`. `GET /api/sessions` gives
each session's `collection` (or null).

### Collection test harness

`scripts/run_collection_test.py` creates or resumes a collection (default: "Test 1 - XTB campaign" from
session `20261017-192847-808860`, "XTB · 1") and runs each run in a worker subprocess, K at a time:

1. sets the run's LLM (default `claude_code` / `claude-opus-5-5`);
2. checks the copied preprocessing (keyframes, Whisper transcript) is there; it is never re-run;
3. vision features: `--vision shared` (default) computes them once, in run 001, and copies them into
   every run, so the runs sample the scoring prompts only (P5 never sees the features, so per-run vision
   noise would only blur P6); `--vision per-child` makes each run compute its own;
4. clears the run's own P6 / P5 answers and runs one iteration of the active prompt against P5
   (fresh scoring: each run is an independent sample), then asks the adoption gate.

Accuracy comes from each run's iteration comparison (`evaluate.compare_iteration`, the only code that
opens the answer keys); the script reads only the iteration records. Progress is saved after every run:
finished runs are skipped on the next start, failed or interrupted ones retried (`--skip-errors` not to).
A usage limit stops new runs; start again after the reset. The report (`report.json`, `report.md`, also
printed) has each video's verdict distribution across runs, accuracy vs the CAC outcome (mean ± stdev),
false SCALE, winners caught, P5 / P6 agreement, the gate pass rate, timing per run and per call,
list-price cost and failures.

```bash
.venv/bin/python scripts/run_collection_test.py --children 100 --concurrency 2 --limit 2   # pilot
.venv/bin/python scripts/run_collection_test.py --children 100 --concurrency 2           # all, or resume
.venv/bin/python scripts/run_collection_test.py --report-only
```

Options: `--source`, `--name`, `--provider`, `--model`, `--prompt` (default: each run's active prompt),
`--vision shared|per-child`, `--limit`, `--concurrency`, `--skip-errors`, `--report-only`.

## Runs

A **run** is one pipeline execution with a fixed prompt version, provider and model, recorded in `session.json` under
`"run"` (prompt id + hash, provider, model, status `running`/`partial`/`done`, videos scored, errors).
**Start pipeline** (Dataset tab) decides what to do and asks for confirmation before any model call
(`GET /api/pipeline/plan` shows the same text):

| The session has | Start does |
|---|---|
| no run yet | runs in place and records the first run |
| an unfinished run (some videos failed, or the server stopped) | resumes it in place, same prompt and model; if you changed the prompt or model it asks: resume, or create a new run |
| a completed run | creates a new child session ("run 002", ...) and runs there; a plain session is first wrapped in an auto-created parent collection and stays as run 001 |

A new run copies the dataset, clips, keyframes, transcripts and the locked ground truth. It does **not** copy score
caches, iterations, recaps, timings, review sheets or decisions, so a run is honest. Vision features are reused only
when the new run's provider and model are the same as the run it branches from (vision does not depend on the prompt);
otherwise vision runs again. Same prompt and model as an existing run is allowed (a repeat run, for variance) and is noted.
A start that cannot begin deletes the child it just made.

Each session's video list, review card and Evaluation follow **its own run's prompt**; the Dataset tab's *Prompt for the next
run* only chooses the prompt of the next run. The parent dashboard lists every run (the original is run 001) with its prompt,
provider and model, and averages each prompt separately. A session saved before runs were recorded is inferred from its score
caches on read and never rewritten. `scripts/score_video.py` remains a low-level in-place scorer and records no run.

## Prompt versions and iterations

The tuning loop of the workflow design (Section 6): after a campaign the data/AI team reads the error
report (or the prompt pack), hand-tunes P6, and a new version is adopted only if it beats P5.
Versions are made in the top-level **Prompts** area (`#/prompts`); running iterations and adopting them is in a campaign's Evaluation tab → *Prompts for this campaign*.

- **Versions** (`app/prompt_versions.py`) live in one global library, `uploads/_prompts/library.json` (atomic writes, ids
  allocated under a lock, so a version is created once and any session can run it; it survives restarts like sessions do).
  Per session, `session.json` keeps only `"prompts": {active, adoptions}`. Sessions saved before the library existed are
  imported once (keyed by SHA-256; a clashing id with different text gets the next free id, and that session's stale cache for
  the old id is dropped). Deleting a version is refused while a run is in progress or a session has it active. Built in: `P5`
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
python scripts/score_video.py <session_id> --all --prompt P6.2   # score with that version; does not adopt it
```

`tests/` checks the integration offline (fake clients, no key, no network): the request sent,
structured-output requests, refusal handling, response parsing, the rule guard and the injection guard;
the other providers, prompt versions and iterations; and the Claude Code CLI provider (a fake
subprocess: flags, environment, images, retries, errors), collections and the collection harness
(`tests/test_claude_code.py`). `pip install pytest`, then `python -m pytest -q tests` (90 tests).
