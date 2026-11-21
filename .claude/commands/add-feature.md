---
description: Add a feature to the PartGram workflow (full-workflow-api): implement, test offline, QA
argument-hint: <feature description>
---

Add this feature to the PartGram workflow: $ARGUMENTS

Follow these steps in order:

1. Spawn the `senior-ai-system-developer` agent with the feature description. Tell it to:
   - read `CLAUDE.md` and the code/tests involved first;
   - pick the smallest change that fully solves it, matching the existing pipeline stage pattern
     (stage caches in `runs/<video_id>/`, `force=true` recompute, `None` not 0 for missing metrics);
   - NOT edit P5 (`app/p5_template.txt`), locked ground truth, or call any LLM API (offline tests use the fake client);
   - add or extend offline tests in `full-workflow-api/tests/`;
   - update `full-workflow-api/README.md` / `CLAUDE.md` only if behaviour or commands change;
   - return: files changed, tests added, test result, open questions.
   If the request is ambiguous in a way that changes the design, it must return the question with a recommended answer instead of guessing.
2. When it finishes, spawn the `qa` agent to verify the change: run
   `cd full-workflow-api && ../.venv/bin/python -m pytest -q tests`, check repo invariants, and report bugs.
   QA does not fix production code; send any bugs back to `senior-ai-system-developer`.
3. Report to the user: what changed, test results, and anything left open. Do not commit unless asked.
