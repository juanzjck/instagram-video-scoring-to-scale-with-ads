# Section 9 submission checklist

Deliverable: one Word/PDF document, 3–5 pages including charts. Built PDF: **4 pages** (LibreOffice render).
Author: "Juan Salzar" (taken from `FirstTest-prompts/report/report.md`; `git config user.name` is unset).

## Rubric coverage

| Criterion | Weight | Where addressed (section · page) | Status |
|---|---|---|---|
| Clarity and Organization | 30 | Summary + sections 1–6 follow brief parts 1–5 in order; Tables 1–2, Figs 1–2 captioned · pp. 1–4 | ✅ |
| Depth of Content and Critical Thinking | 40 | §2 data/model/output bias with metrics + permutation tests (p. 1–2); proxy leakage via discount codes; audit of own detectors (lexicon false alarm, grounding parser artefact) (pp. 2–3); trade-offs of rule guard and post-processing (p. 3); limitations (p. 4) | ✅ (fair_prompt condition missing, see TODO) |
| Application and Relevance | 30 | Real PartGram pipeline: `flatten.py`, `scoring.py` rule guard replay, `guard.py`, review card, `decisions.jsonl`, GT lock (pp. 2–3); CAC vs target CAC, EC/CO markets, creator income (pp. 1, 4) | ✅ |
| Brief part 1 – concepts | — | §1 · p. 1 | ✅ |
| Brief part 2 – data / model / output bias | — | §2.1–2.3 · pp. 1–2 | ✅ |
| Brief part 3 – detection, mitigation, metrics, inclusive data | — | §3, Table 2 · pp. 2–3 | ⚠️ fair_prompt not run |
| Brief part 4 – XAI, auditing, user feedback | — | §4 · p. 3 | ✅ (appeal flow is proposed, not built) |
| Brief part 5 – documentation + evaluation | — | §5–6 · p. 4 | ✅ |

## Files

| File | What it is |
|---|---|
| `Section9_Bias_Fairness_Transparency_Report.docx` | The submission (Word) |
| `Section9_Bias_Fairness_Transparency_Report.pdf` | Same, PDF (4 pages) |

Sources (not submitted): `../report/Section9_Bias_Fairness_Transparency_Report.md`, `../report/section9_analysis.py`,
`../report/tables/*.csv`, `../report/figures/*.png`, `../report/reference.docx`, `../report/outline.md`,
`../outputs/` (real run, claude-sonnet-4-6), `../outputs_previous_run/`. No `.env` is included.

## Reproduce (offline, free)

```bash
cd capstone-section9-bias
../.venv/bin/python report/section9_analysis.py          # tables + figures from cached answers
cd report
pandoc Section9_Bias_Fairness_Transparency_Report.md -o Section9_Bias_Fairness_Transparency_Report.docx \
  --reference-doc=reference.docx --resource-path=.:..:../outputs
soffice --headless --convert-to pdf Section9_Bias_Fairness_Transparency_Report.docx --outdir .
```

## Open TODOs / paid runs (not executed)

1. **fair_prompt condition** (Table 2 row "Fairness rules in the prompt"). Fill `FAIR_SYSTEM` (TODO 1) in
   `bias_audit.py`, then run (≈80 new API calls; baseline and blind come from cache):
   `cd capstone-section9-bias && ../.venv/bin/python bias_audit.py && ../.venv/bin/python report/section9_analysis.py`
   then update Table 1/2 and rebuild.
2. Optional: repeated samples per persona to separate sampling noise from persona effects (cache returns one answer
   per prompt; needs a code change to bypass the cache).
3. `bias_audit.py` TODO 3/4 (metrics, thresholds) remain unfilled; the report's metrics come from
   `report/section9_analysis.py` instead.
