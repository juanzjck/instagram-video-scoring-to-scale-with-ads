# Capstone Section 9 — Bias, Fairness, and Transparency in PartGram's AI Scorer
<your name> · <date> · 3–5 pages, Word/PDF

## 1. Concepts (½ p)
- Data, algorithmic/model and societal bias — one line each with a PartGram example.
- Fairness metrics: demographic parity, equalized odds, disparate impact.
- Why transparency matters when AI decides who gets paid ad budget (like hiring or credit).

## 2. Potential biases in PartGram's solution (1–1½ p) — qualitative + quantitative
- **Data bias** (`data_audit.txt`): Ecuador missing as a main audience; no creators < 10K
  followers; Spanish-dominant; 10 synthetic videos; labels from past scaling decisions
  (feedback loop: only scaled videos get ROAS). Engagement reflects Instagram's own ranking. TODO table.
- **Model bias** (`fairness_metrics.csv`, `scale_rate.png`): SCALE rate and score by gender
  and country with identical videos. Also: keyframes + Whisper transcripts expose face,
  voice and accent in the real pipeline. TODO numbers.
- **Output bias** (word use): appearance vs competence words in feedback by gender. TODO.

## 3. Strategies to mitigate bias (1 p)
- Bias detection tool: `bias_audit.py` run on every prompt/model change (target: DI ≥ 0.80, score gap ≤ 5).
- Mitigation tested: blind profile (pre-processing), fairness rules (algorithmic),
  per-group thresholds (post-processing) — before/after table. TODO
- Proposed: re-weighting / re-sampling the evaluation set across EC/CO, niches and follower
  tiers; inclusive data collection with `instagram-video-downloader`.

## 4. Transparency (1 p)
- Explainability: reasons must cite real numbers — `grounded_reasons` result; the review card
  in `full-workflow-api` shows reasons + rule-guard checks. Why LIME/SHAP don't fit an API LLM
  and what replaces them (grounded reasons, counterfactual stability, fixed rubric).
- Model auditing: `audit_log.jsonl` (model, prompt hash, metrics per run); monthly review.
- User feedback: creators/brands can appeal DO_NOT_SCALE → human review; "report a concern" option.
- Disclosure to creators that AI assists and a human decides.

## 5. Evaluation (½ p)
Fairer verdicts → creator retention (10K→20K target) and brand trust → repeat budgets;
less legal risk under Ecuador LOPDP / Colombia Ley 1581. Remaining risks and next steps.
