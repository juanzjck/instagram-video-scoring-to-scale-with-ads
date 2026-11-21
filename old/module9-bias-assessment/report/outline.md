# Outline: Bias_Mitigation_Report (Module 9). Target is 4 pages, about 1,900 words plus 2 figures and 4 tables

Rubric weights: Code Quality 40 · Application of Concepts 40 · Evaluation/Testing/Documentation 20
(Assignment criteria: Detection 40 · Mitigation 40 · Documentation 20)

| # | Section | Weight served | Target | Evidence |
|---|---|---|---|---|
| 1 | Model and use case | Concepts | 150 w | bias_detection_final.py header, outputs_final/run_info.json |
| 2 | Method: name audit, conditions, metrics | Concepts / Code | 300 w + table | bias_detection_final.py (NAMES, RESUMES, fairness(), name_effect()) |
| 3 | Biases detected (baseline) | Detection 40 | 450 w + table + fig | outputs_final/fairness_metrics.csv, name_effect.csv, word_use.csv, word_tests.csv, baseline.csv (quotes) |
| 4 | Mitigation strategies | Mitigation 40 | 250 w + table | FAIR_SYSTEM, equalize_thresholds(), run("blind") |
| 5 | Effectiveness and trade-offs | Mitigation 40 | 400 w + 2 tables + fig | outputs_final/*, outputs_mock/* (planted-bias validation) |
| 6 | Fairness metrics, testing, challenges | Testing/Doc 20 | 200 w | test_bias_detection.py (11 tests) |
| 7 | Ethical reflection | Doc 20 | 200 w | none needed (discussion) |
| 8 | Limitations | Doc 20 | 120 w | n, single sample, cache determinism |
| | References | | 4 refs | only those cited |

Open: fair_prompt real results (paid run, 64 calls) and the author name.
