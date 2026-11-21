# Submission checklist: Module 9 Bias Detection and Mitigation

Prepared 2026-11-01. Source folder: `module9-bias-assessment/` (all evidence comes from here).

## Rubric coverage

| Criterion | Pts | Where addressed | Status |
|---|---|---|---|
| Code Quality and Functionality | 40 | `Bias_Detection_Code/bias_detection_final.py` (documented functions, OFFLINE/MOCK/real modes, cache) | ✅ |
| Application of Concepts: detection (DP, DI, equalized odds, word metrics) | 40 | Report §2 (p1), §3 Tables 1–2 + Fig 1 (p2–3); code `fairness()`, `name_effect()`, `word_use()` | ✅ |
| Application of Concepts: mitigation (pre-, algorithmic, post-processing) and trade-offs | (40) | Report §4–5, Tables 3–4 + Fig 2 (p3–4); code `run("blind")`, `FAIR_SYSTEM`, `equalize_thresholds()` | ⚠️ `fair_prompt` real results pending (paid run) |
| Evaluation, Testing, Documentation | 20 | `test_bias_detection.py` (11 tests incl. edge cases); report §6 (p4), §7 ethics (p4–5), §8 limitations (p5); code README | ✅ |
| Report: model/use case, biases, mitigation, metrics/challenges, ethics | — | §1, §3, §4–5, §6, §7 | ✅ |
| Report length 3–5 pages | — | 5 pages (LibreOffice render of the .docx) | ✅ (check in Word) |
| File labels | — | `Bias_Detection_Code.zip`, `Bias_Mitigation_Report.pdf/.docx` | ✅ |

## Files

| File | What it is |
|---|---|
| `Bias_Mitigation_Report.pdf` | Report (5 pages). Submit this, or the .docx |
| `Bias_Mitigation_Report.docx` | Same report as an editable Word file |
| `Bias_Detection_Code.zip` | Zipped `Bias_Detection_Code/` folder (no `.env`) |
| `Bias_Detection_Code/bias_detection_final.py` | Main code (completed template) |
| `Bias_Detection_Code/test_bias_detection.py` | 11 offline tests |
| `Bias_Detection_Code/bias_detection.py` | Original template, unchanged |
| `Bias_Detection_Code/outputs/cache/` | 72 cached real answers from claude-sonnet-4-6 |
| `Bias_Detection_Code/outputs_final/` | Real-model results (CSV + chart) |
| `Bias_Detection_Code/outputs_mock/` | Mock planted-bias results (synthetic) |

## Reproduce (free)

```bash
cd module9-bias-assessment
OFFLINE=1 ../.venv/bin/python bias_detection_final.py
MOCK=1    ../.venv/bin/python bias_detection_final.py
../.venv/bin/python test_bias_detection.py
cd report && pandoc Bias_Mitigation_Report.md -o Bias_Mitigation_Report.docx --resource-path=.:.. --reference-doc=reference.docx \
  && soffice --headless --convert-to pdf Bias_Mitigation_Report.docx --outdir .
```

## Open TODOs

1. **Author name.** `git config user.name` is unset. Replace `TODO: student name` in `report/Bias_Mitigation_Report.md` (front matter) and rebuild.
2. **fair_prompt real run (paid, 64 Claude calls).** The baseline and blind answers are cached, so only the 64 fair_prompt prompts are sent:
   ```bash
   cd module9-bias-assessment && ../.venv/bin/python bias_detection_final.py
   ```
   Then fill the `fair_prompt` row of Table 3 and the sentence under it (report §5) from
   `outputs_final/fairness_metrics.csv` and `name_effect.csv`. Remove the fair_prompt bullet in §8,
   rebuild the docx/pdf, and copy `outputs_final/`, `outputs/cache/` and the report into `submission/` again.
3. Optional: open the .docx in Word and confirm it still fits in 5 pages.
