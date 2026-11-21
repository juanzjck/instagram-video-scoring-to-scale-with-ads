# Capstone Section 9 — Bias, Fairness & Transparency (PartGram)

Section 9 asks for a **document** (3–5 pages). `bias_audit.py` produces the evidence for it,
using the project's own data (`../data-example`) and scorer setup.

| Section 9 part | Evidence from `bias_audit.py` | Output file |
|---|---|---|
| 2. Data bias | `data_audit()` — niches, audience countries, language, label balance, follower tiers | `outputs/data_audit.txt` |
| 2. Model bias | counterfactual test: same video, creator gender (M/F) × country (EC/CO) changed | `fairness_metrics.csv`, `scale_rate.png` |
| 2. Output bias | `word_use()` — appearance vs competence words in creator feedback | console / `baseline.csv` |
| 3. Mitigation + metrics | `blind`, `fair_prompt`, per-group thresholds; DP, DI, equalized odds | `fairness_metrics.csv` |
| 4. Transparency | `transparency_check()` — are reasons grounded in real numbers? are verdicts stable across personas? `audit_log.jsonl` — one record per run | `transparency.csv`, `audit_log.jsonl` |

## Steps

1. **Finish Module 9 first.** TODOs 3–4 here are the same metric and threshold functions —
   copy them from `bias_detection.py`.
2. **Dry run (free):**
   ```bash
   cd ~/Documents/git/capstone-partgram-ai-practice/capstone-section9-bias
   MOCK=1 ../.venv/bin/python bias_audit.py
   ```
   Then `rm -rf outputs`.
3. **Fill the TODOs:** 1 fairness rules · 2 any extra data-audit table · 3 metrics · 4 thresholds.
4. **Real run:** `../.venv/bin/python bias_audit.py`. About 80 baseline calls plus 80 with
   fairness rules; the blind condition sends identical text per video, so it costs about 10.
   Key and `LLM_MODEL` come from `.env` in this folder.
5. **Write the document** from `Section9_outline.md`. Paste the tables and `scale_rate.png`.

Already visible from the data audit (no API needed): no creator in `data-example` has
Ecuador as their main audience country even though PartGram operates there, there are no
creators under 10K followers, and only 3 of 10 videos are labelled SCALE. Those are your
data-bias findings for Part 2.
