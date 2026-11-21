# Outline — Capstone Section 9 report (target 4–5 pages, ~2,000 words + 2 charts + 4 tables)

Builds on `../Section9_outline.md`. Rubric: Clarity & Organization 30 · Depth & Critical Thinking 40 ·
Application & Relevance 30. Every number comes from `../outputs/` or `tables/` (made by
`section9_analysis.py`, offline).

| # | Section | Brief part | Rubric weight it mainly serves | Target | Evidence |
|---|---|---|---|---|---|
| 0 | Summary | 5 | Clarity | 120 w | all below |
| 1 | Concepts and why they matter for PartGram | 1 | Depth, Application | 250 w | references |
| 2 | Bias identification (data, model, output) | 2, 5 | Depth (largest) | 700 w + table + Fig 1 | `../outputs/data_audit.txt`, `tables/fairness_metrics_full.csv`, `tables/permutation_tests.csv`, `tables/per_video_scores.csv`, `tables/previous_run_verdict_flips.csv`, `tables/word_use.csv`, `../outputs/audit_log.jsonl`, `../../data-example/transcripts.csv` |
| 3 | Mitigation strategies and fairness metrics | 3, 5 | Depth, Application | 500 w + table | `tables/fairness_metrics_full.csv`, `tables/rule_guard_replay.csv`, `../../full-workflow-api/app/flatten.py`, `app/scoring.py`, `app/guard.py` |
| 4 | Transparency and explainability | 4, 5 | Application | 450 w + Fig 2 | `tables/grounded_reasons_summary.csv`, `../outputs/transparency.csv`, `app/static/app.js`, `app/main.py` (decide), `app/dataset.py` (lock), `app/evaluate.py` |
| 5 | Evaluation: fairness, trust, business outcome | 5 | Application, Depth | 250 w | CAC vs target CAC (`../../data-template/README.md`) |
| 6 | Limitations and next steps | — | Depth | 150 w | — |
| — | References | — | Clarity | short | only sources cited |

Not used as evidence: `module9-bias-assessment/` (résumé screener, different use case) — one-line
cross-reference only. `_to_delete/` — ignored.
