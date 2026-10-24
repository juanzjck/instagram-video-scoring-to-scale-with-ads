# PartGram AI practice (capstone)

PartGram is a Latin American marketplace connecting brands with content creators. Brands pay
creators to post Instagram Reels; the best-performing organic videos then get paid ad budget
("scaled"). This repo explores using Claude to make that scale / don't-scale decision **before
any budget is spent**, and to measure whether the decision would have hit the brand's target CAC.

The work grew in stages: a prompt-engineering study on text-only summaries, then a multimodal
pipeline with a review UI, then tools to collect real Instagram data and evaluate against real
outcomes.

## Repository layout

| Path | What it is |
|---|---|
| `FirstTest-prompts/` | Stage 1: prompt engineering. Five prompts (P1–P5) run over synthetic videos, scored with a rubric. Notebook, outputs, scoring sheets and the written report. |
| `data-example/` | Deterministic synthetic dataset (10 videos, one per decision scenario) plus example media. `make_dataset.py` regenerates the CSVs. |
| `full-workflow-api/` | Stage 2: FastAPI service + review-card UI. Video → keyframes + Whisper transcript → Claude vision → P5/P6 scoring → rule and injection guards → human decision → campaign recap video → evaluation against ground truth. |
| `data-template/` | Empty CSVs with the exact headers `full-workflow-api` expects (including CAC columns), with a column-by-column guide. |
| `instagram-video-downloader/` | Collects real Instagram posts/reels: downloads video and thumbnail, gathers available metrics and comments, writes CSVs shaped like `data-template/`. |
| `Case_study_AI_creative_scoring_Kellanova_Vidmob.md` | Background case study on AI creative scoring in marketing. |
| `*.docx`, `Claude outputs/` | Written course deliverables (multimedia integration section, weekly learning log). |

Each subproject has its own README with full details:
[full-workflow-api](full-workflow-api/README.md) ·
[data-template](data-template/README.md) ·
[instagram-video-downloader](instagram-video-downloader/README.md).

## How the pieces fit

```
instagram-video-downloader ──► CSVs (data-template format) ──┐
data-template (filled by hand) ─────────────────────────────►├─► full-workflow-api
data-example (synthetic) ───────────────────────────────────┘     Dataset tab → pipeline → review card
                                                                   → decision log → Evaluation (vs ground_truth.csv)
FirstTest-prompts: origin of flatten() and the P5 prompt reused by the API
```

## Quick start

Python 3 (the repo venv was built with 3.14). A single venv at the repo root is used by the notebook and the API.

```bash
python3 -m venv .venv
.venv/bin/pip install -r FirstTest-prompts/requirements.txt
.venv/bin/pip install -r full-workflow-api/requirements.txt

cp full-workflow-api/.env.example full-workflow-api/.env   # add ANTHROPIC_API_KEY
cd full-workflow-api
../.venv/bin/uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 and start in the **Dataset** tab. Offline tests (no key, no network):

```bash
cd full-workflow-api && ../.venv/bin/python -m pytest -q tests
```

To collect real data, see [instagram-video-downloader](instagram-video-downloader/README.md)
(it uses its own venv).

## Key ideas

- **Blank is not zero.** Instagram returns empty values for unavailable metrics; every stage
  renders them as `not available`, never `0`.
- **Rule guard in code.** Under 48 h since posting, reach under 1,000, or a missing core metric
  → `DEFER_TO_HUMAN`, regardless of the model's answer.
- **Prompt-injection guard.** Transcripts, on-screen text and comments are delimited, scanned and
  redacted before scoring.
- **Locked ground truth.** Real outcomes (ad spend, conversions, CAC vs target CAC) are locked
  with a hash before scores are viewed, then used to evaluate P5 vs P6.
- **Human in the loop.** The model recommends; a reviewer decides, and every decision is logged.

## Secrets

`.env` files are git-ignored; only `.env.example` files are committed. Keys used:
`ANTHROPIC_API_KEY` (required), `OPENAI_API_KEY` (optional, recap voice / raster images),
`IG_ACCESS_TOKEN` and `IG_USER_ID` (optional, Instagram Graph API).
