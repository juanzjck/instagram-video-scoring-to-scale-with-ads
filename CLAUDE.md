# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Capstone coursework for PartGram, a Latin American brand ↔ creator marketplace. Claude decides whether a
creator's Instagram Reel should get paid ad budget (SCALE / DO_NOT_SCALE / DEFER_TO_HUMAN) before any
spend, and results are measured against real outcomes (CAC vs target CAC). It is a set of independent
subprojects built in stages, plus course deliverables (`*.docx`, `report/` folders); see `README.md`.

## Environment

- One shared venv at the repo root (`.venv`, Python 3.14) for everything **except**
  `instagram-video-downloader/`, which has its own `.venv`.
- Run scripts with `../.venv/bin/python` from inside a subproject (the system `python` lacks the deps).
- `ANTHROPIC_API_KEY` lookup: `full-workflow-api/app/config.py` reads `full-workflow-api/.env`, then falls
  back to `FirstTest-prompts/.env`. `bias-fairness-module9/bias_study.py` reads `bias-fairness-module9/.env`.
  `LLM_MODEL` overrides the model in both. In `full-workflow-api` the provider is selectable too
  (`LLM_PROVIDER` = anthropic | openai | gemini | ollama | openai_compatible, plus a per-session choice in
  the UI / `PUT /api/llm`); see its README "Choosing the LLM". Model answers are cached per model in
  `runs/<video_id>/llm/<provider>--<model>/`. A 401 `invalid x-api-key` means the key itself is dead,
  not a path problem — test with a free `models.list()` call.

## Commands

```bash
# install
.venv/bin/pip install -r FirstTest-prompts/requirements.txt -r full-workflow-api/requirements.txt

# full-workflow-api: web app (UI at :8000, OpenAPI at :8000/docs)
cd full-workflow-api && ../.venv/bin/uvicorn app.main:app --reload

# full-workflow-api: offline tests (fake Anthropic client, no key/network)
cd full-workflow-api && ../.venv/bin/python -m pytest -q tests
../.venv/bin/python -m pytest -q tests/test_integration_offline.py::test_complete_raises_on_refusal   # single test

# full-workflow-api: CLI pipeline (no UI)
../.venv/bin/python scripts/score_video.py --list
../.venv/bin/python scripts/score_video.py <session_id> --all [--force] [--summary] [--prompt P6.2]
../.venv/bin/python scripts/make_synthetic_media.py --dataset=<id> [--ai-images] [--force] vid_001

# full-workflow-api: collection test (N runs of one session via the `claude` CLI, subscription login)
../.venv/bin/python scripts/run_collection_test.py --children 100 --concurrency 2 [--limit 2]   # resumable
../.venv/bin/python scripts/run_collection_test.py --report-only

# bias study (Module 9)
cd bias-fairness-module9
MOCK=1 ../.venv/bin/python bias_study.py      # free dry run, fake scorer with a planted gender penalty
../.venv/bin/python bias_study.py             # real run; answers cached in outputs/cache/

# synthetic dataset
../.venv/bin/python data-example/make_dataset.py

# instagram collector (own venv)
cd instagram-video-downloader && .venv/bin/python ig_collect.py urls.txt --out output
```

No linter or build step is configured.

## Architecture

**Data contract.** `data-template/` defines the CSV headers (`videos`, `creators`, `campaigns`,
`comments`, `ground_truth`, …) everything else speaks. `data-example/` (synthetic, 10 videos = one per
decision scenario) and `instagram-video-downloader/` (real) both produce CSVs in that shape; the API
and `bias_study.py` consume them.

**Lineage.** `FirstTest-prompts/` (notebook, prompts P1–P5, rubric) is the origin of `flatten()` and the P5
prompt. `full-workflow-api` ports both **verbatim** (`app/flatten.py`, `app/p5_template.txt`); P6 is
derived from P5 by string surgery in `app/prompts.py` so the two cannot drift. Don't edit P5 to
"improve" it — it is the fixed baseline P6 is evaluated against. Tuned prompts are saved as immutable versions
(`P6.2`, `P6.3`, … in one global library, `uploads/_prompts/library.json`, `app/prompt_versions.py`; UI: top-level *Prompts*, route `#/prompts`; the active version and adoption log stay per session in `session.json`); an iteration (`app/iterations.py`) runs a
version next to P5 and freezes verdicts in `runs/iterations/`; a version is adopted only if it beats P5
(no more false SCALE, then accuracy) or with a logged written override.

**full-workflow-api pipeline** (`app/pipeline.py` orchestrates, `app/main.py` is FastAPI + routes):
ingest (`ingest.py`) → keyframes + local Whisper transcript (`media.py`, ffmpeg via `imageio-ffmpeg`) →
one Claude vision call per video (`vision.py`) → `flatten()` + `=== CONTENT FEATURES ===` section →
P5/P6 scoring (`scoring.py`, all API calls via `llm.py`) → rule guard → injection guard (`guard.py`) →
review card (`app/static/`) → human decision log → recap video (`recap.py`, `genmedia.py`) →
evaluation (`evaluate.py`).

- **Sessions = campaigns.** State lives in `uploads/<id>/` (`session.json`, generated CSVs, `runs/`).
  Every server start is clean; nothing is bundled. Each stage caches JSON in `runs/<video_id>/`;
  `force=true` recomputes. Regenerating a dataset drops cached results/recaps but keeps clips and decisions.
- **Rule guard runs in code after the model answers**: < 48 h since post, reach < 1,000, or a missing
  core metric → `DEFER_TO_HUMAN`; the model's verdict is kept alongside. (P5 itself says ~24 h; P6 and
  the guard use 48 h — intentional.)
- **Blank is not zero.** Missing metrics are `None` end to end and render `not available`, never `0`.
- **Ground truth is locked** (SHA-256, timestamped copy) before scores are viewed; the API then returns
  409 on changes. `evaluate.py` is the only module that opens answer keys.
- Claude has no audio I/O: transcription is local Whisper; recap voice is OpenAI TTS if
  `OPENAI_API_KEY` is set, else macOS `say`. Charts, badges and captions are drawn with Pillow, never generated.
- No sampling parameters are sent to the model (any provider). `app/llm.py` is the only module that talks to
  an LLM; `openai` / `google-genai` are optional imports.
- **`claude_code` provider** runs `claude -p` with clean-context flags (`--safe-mode`, no settings / MCP /
  tools, its own `--system-prompt`, an empty temp cwd, no `ANTHROPIC_API_KEY` in its env). Not `--bare`: that
  drops the subscription login. Images go in on stdin via `--input-format stream-json`.
- **Runs** (`app/runs.py`): a run is one pipeline execution with a fixed (prompt version, provider, model), recorded in
  `session.json` under `"run"` (`{n, prompt_id, prompt_sha256, provider, model, status, scored, videos, errors, ...}`).
  *Start pipeline* (`GET /api/pipeline/plan` previews it, `POST /api/pipeline/start` does it; the UI confirms first): no run
  yet → runs in place; an unfinished run (`partial`) → resumes in place (or asks when the prompt/model changed); a completed run
  → creates a new child session (the original becomes run 001 of an auto-created parent collection, `source_is_run`) and runs
  there. A child copies the dataset, clips, keyframes/transcripts and the locked ground truth, but no score caches,
  iterations, recaps, timings, review sheets or decisions; vision features only when provider + model are the same as the run it
  branches from. Views (video list, card, Evaluation, process) default to the session's own run prompt; the Dataset tab's selector
  only chooses the prompt of the NEXT run. Sessions without a record are inferred on read from their score caches, never rewritten.
  `scripts/score_video.py` stays a low-level in-place scorer (no run recorded).
- **Collections** (`app/collection.py`): a parent in `uploads/_collections/<id>/` groups copies ("runs") of one
  session; each run's `session.json` has `"collection"`. `scripts/run_collection_test.py` drives them.

**bias-fairness-module9.** Single script `bias_study.py`: 10 videos × 6 personas (3 women / 3 men,
surname-matched) with identical metrics; only name + gender line differ. Conditions: `baseline`,
`blind` (profile removed, pre-processing), `fair_prompt` (fairness rules in system prompt), and
`post_threshold` (per-gender score cut-off, post-processing — uses gender explicitly). Metrics:
demographic parity diff, disparate impact ratio, TPR/FPR gaps, and gendered-word counts in the Spanish
feedback. The cache key is a hash of model + system + user prompt, so changing a prompt invalidates only
that condition. When editing `WORDS`, list both grammatical genders and avoid words the model uses
for the content rather than the person (e.g. "natural", "energía"), or the word metric reports false bias.
`README.md` there still describes the TODOs and the old `FirstTest-prompts/.env` key path; both are outdated.
