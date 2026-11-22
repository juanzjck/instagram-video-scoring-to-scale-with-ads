# Bias_Detection_Code (Module 9)

Claude used as a résumé screener: 8 résumés x 8 gender/race-coded names = 64 outputs per condition.
The conditions are baseline, blind (pre-processing), fair_prompt (fairness rules) and post_race
(post-processing thresholds).

| File | What it is |
|---|---|
| `bias_detection_final.py` | Main code. Generates outputs and computes fairness metrics (DP, DI, TPR/FPR gaps, score gap with permutation p, counterfactual consistency, word use). It also applies the three mitigations. |
| `test_bias_detection.py` | 11 offline tests (hand-computed metrics, edge cases, mock end-to-end, offline guard). |
| `bias_detection.py` | Original course template, kept unchanged for reference (the tests check prompt identity against it). |
| `outputs/cache/` | Cached real answers from `claude-sonnet-4-6` (64 baseline + 8 unique blind). |
| `outputs_final/` | Results from the real model, regenerated from cache (`OFFLINE=1`). |
| `outputs_mock/` | Results from the mock scorer with a planted race penalty (`MOCK=1`); synthetic. |

```bash
pip install -r requirements.txt
OFFLINE=1 python bias_detection_final.py   # free, rebuilds outputs_final/ from the cache
MOCK=1    python bias_detection_final.py   # free, rebuilds outputs_mock/
python test_bias_detection.py              # 11 tests, free
# paid: needs ANTHROPIC_API_KEY in .env (default model claude-sonnet-4-6); only uncached prompts are sent
python bias_detection_final.py
```
