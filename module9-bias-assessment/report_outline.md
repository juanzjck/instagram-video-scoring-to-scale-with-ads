# Bias_Mitigation_Report — AI Résumé Screening
<your name> · <date> · 3–5 pages · Rubric: Detection 40 · Mitigation 40 · Documentation 20

## 1. Model and use case (½ p)
Claude (`claude-sonnet-5-5`, Anthropic API, JSON output) screens applicants for a Junior Data
Analyst role: INTERVIEW/REJECT + score + feedback. Hiring is a high-stakes domain where bias
causes direct harm and legal risk (EEOC four-fifths rule).

## 2. Method (½ p)
8 résumés (4 qualified = ground truth) × 8 invented names (2 per gender × race group) = 64.
Name audit (Bertrand & Mullainathan, 2004). Limitations: small n, names as the only signal,
binary gender, US-centric race coding.

## 3. Biases detected — baseline (1 p)  ← 40 pts
- Table: interview rate per group, dp_diff, di_ratio, TPR/FPR gaps, score gap. TODO
- Chart: `interview_rate.png`. TODO
- Text: agentic vs communal words by gender/race + 2–3 quoted feedback examples. TODO
- Interpretation (size of the gaps, whether meaningful at n=64). TODO

## 4. Mitigation strategies (1 p)  ← 40 pts
| Strategy | Family | What changed |
|---|---|---|
| Blind name | Data / pre-processing | name replaced by "Candidate" |
| Fairness rules | Algorithmic | rules in the system prompt |
| Group thresholds | Post-processing | per-group score cut-offs |
Why not re-sampling training data: closed API model; we control input, instructions, output.

## 5. Effectiveness and trade-offs (1 p)
Before/after table on the same metrics; accuracy vs ground truth; blinding fails with proxies
(school, address, clubs); thresholds equalise rates but treat groups differently on purpose;
demographic parity and equalized odds can conflict. TODO

## 6. Ethical reflection and challenges (½ p)
Human review of every REJECT; audits per model/prompt version; candidate notice and appeal;
limits of name-based testing.

## References
Bertrand & Mullainathan (2004), *Are Emily and Greg More Employable than Lakisha and Jamal?*;
Barocas, Hardt & Narayanan, *Fairness and Machine Learning*; EEOC Uniform Guidelines (four-fifths rule).
