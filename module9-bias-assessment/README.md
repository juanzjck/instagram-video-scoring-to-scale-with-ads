# Module 9 — Bias Detection and Mitigation (standalone example)

**Use case:** Claude as a résumé screener for a Junior Data Analyst job.
**Test:** 8 identical résumés (4 qualified, 4 not) × 8 names signalling gender and race = 64 outputs.
Only the name changes, so any gap in decisions is bias (name-audit method, Bertrand & Mullainathan 2004).

| Assignment step | Where |
|---|---|
| Choose model & use case | Claude + résumé screening (top of `bias_detection.py`) |
| Generate outputs & identify biases | `run("baseline")` + read the `feedback` column |
| Detect bias with metrics | `fairness()`: demographic parity, disparate impact, equalized odds, score gap; `word_use()` |
| Apply mitigation | `blind` (pre-processing: name hidden), `fair_prompt` (algorithmic: fairness rules), `equalize_thresholds` (post-processing) |
| Evaluate effectiveness | before/after table `outputs/fairness_metrics.csv` + `accuracy` column for the trade-off |
| Document | `report_outline.md` |

## Steps

**1. Setup (5 min)** — any Python 3.10+:
```bash
pip install anthropic pandas matplotlib python-dotenv
cp .env.example .env        # paste your ANTHROPIC_API_KEY
```

**2. Free dry run**
```bash
MOCK=1 python bias_detection.py
```
The mock penalises the Black-coded names, so baseline shows a race gap and blind doesn't.
Then `rm -rf outputs`.

**3. Fill the 5 TODOs (≈1 h)** — résumés (check), fairness rules, word list, the 4 metrics,
threshold post-processing. Re-run with `MOCK=1` and check one metric by hand.

**4. Real run (≈200 calls, under a couple of dollars on Sonnet; cached afterwards)**
```bash
rm -rf outputs && python bias_detection.py
```

**5. Read outputs** — `outputs/baseline.csv` (copy 2–3 feedback examples), `fairness_metrics.csv`,
`interview_rate.png`. Modern models often show small or no gaps: that is a valid finding — say so,
show the numbers, and discuss why (alignment training, small n, explicit name cue).

**6. Write the report** from `report_outline.md` (3–5 pages).

**Submit:** `Bias_Detection_Code` = `bias_detection.py` (+ outputs folder, no `.env`) ·
`Bias_Mitigation_Report.pdf`.
